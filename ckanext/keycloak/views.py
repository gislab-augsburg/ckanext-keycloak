import logging
from flask import Blueprint
from ckan.plugins import toolkit as tk
import ckan.lib.helpers as h
import ckan.model as model
from ckan.common import g
from ckan.views.user import set_repoze_user, RequestResetView
from ckanext.keycloak.keycloak import KeycloakClient
import ckanext.keycloak.helpers as helpers
from os import environ

# for safe return + session handling and logging
from urllib.parse import urlparse
from ckan.common import session as ckan_session, config as ckan_config, request as ckan_request

log = logging.getLogger(__name__)

keycloak = Blueprint('keycloak', __name__, url_prefix='/user')

server_url = tk.config.get('ckanext.keycloak.server_url', environ.get('CKANEXT__KEYCLOAK__SERVER_URL'))
client_id = tk.config.get('ckanext.keycloak.client_id', environ.get('CKANEXT__KEYCLOAK__CLIENT_ID'))
realm_name = tk.config.get('ckanext.keycloak.realm_name', environ.get('CKANEXT__KEYCLOAK__REALM_NAME'))
redirect_uri = tk.config.get('ckanext.keycloak.redirect_uri', environ.get('CKANEXT__KEYCLOAK__REDIRECT_URI'))
client_secret_key = tk.config.get('ckanext.keycloak.client_secret_key', environ.get('CKANEXT__KEYCLOAK__CLIENT_SECRET_KEY'))
scope = tk.config.get('ckanext.keycloak.scope', environ.get('CKANEXT__KEYCLOAK__SCOPE'))
user_name = tk.config.get('ckanext.keycloak.user_name', environ.get('CKANEXT__KEYCLOAK__USER_NAME'))
user_fullname = tk.config.get('ckanext.keycloak.user_fullname', environ.get('CKANEXT__KEYCLOAK__USER_FULLNAME'))

client = KeycloakClient(server_url, client_id, realm_name, client_secret_key, scope)


def _log_user_into_ckan(resp):
    """ Log the user into different CKAN versions.
    CKAN 2.10 introduces flask-login and login_user method.
    CKAN 2.9.6 added a security change and identifies the user
    with the internal id plus a serial autoincrement (currently static).
    CKAN <= 2.9.5 identifies the user only using the internal id.
    """
    if tk.check_ckan_version(min_version="2.10"):
        from ckan.common import login_user
        login_user(g.user_obj)
        return

    if tk.check_ckan_version(min_version="2.9.6"):
        user_id = "{},1".format(g.user_obj.id)
    else:
        user_id = g.user
    set_repoze_user(user_id, resp)

    log.info(u'User {0}<{1}> logged in successfully'.format(g.user_obj.name, g.user_obj.email))


def _safe_return_to():
    """
    Decide a safe URL to return to (same-origin; no auth endpoints).
    Prefers ?came_from (configurable param) over Referer if enabled.
    """
    site_url = (ckan_config.get('ckan.site_url') or '').rstrip('/')

    # Config flags (defaults preserve original behavior = feature off)
    param_name = ckan_config.get('ckanext.keycloak.return_to_param', 'came_from')
    prefer_param = tk.asbool(ckan_config.get('ckanext.keycloak.return_to_prefer_param', True))
    same_origin_only = tk.asbool(ckan_config.get('ckanext.keycloak.return_to_same_origin_only', True))
    raw_disallow = ckan_config.get('ckanext.keycloak.return_to_disallow_paths', '')
    if raw_disallow.strip():
        disallow = tuple(raw_disallow.replace(',', ' ').split())
    else:
        disallow = (
            '/user/sso', '/user/sso_login', '/user/login', '/user/_logout',
            '/user/logged_out', '/user/logged_out_redirect', '/user/reset', '/user/locked'
        )

    source = ckan_request.args.get(param_name) if prefer_param else None
    if not source:
        source = ckan_request.headers.get('Referer')
    target = source or site_url or '/'

    try:
        u = urlparse(target)
        if same_origin_only and site_url and not str(target).startswith(site_url):
            target = site_url or '/'
        if u.path in disallow:
            target = site_url or '/'
    except Exception:
        target = site_url or '/'

    return target


def sso():
    log.info("SSO Login")

    # Only store return target if feature is enabled
    if tk.asbool(ckan_config.get('ckanext.keycloak.enable_return_to', False)):
        try:
            ckan_session['after_login_url'] = _safe_return_to()
            ckan_session.save()
            log.info("after_login_url stored in session: %r", ckan_session.get('after_login_url'))
        except Exception as e:
            log.warning("Could not store after_login_url in session: %r", e)

    try:
        auth_url = client.get_auth_url(redirect_uri=redirect_uri)
    except Exception as e:
        log.error("Error getting auth url: {}".format(e))
        return tk.abort(500, "Error getting auth url: {}".format(e))
    return tk.redirect_to(auth_url)


def sso_login():
    data = tk.request.args
    token = client.get_token(data['code'], redirect_uri)
    userinfo = client.get_user_info(token)
    log.info("SSO Login: {}".format(userinfo))
    if userinfo:
        user_dict = {
            'name': helpers.ensure_unique_username_from_email(userinfo[user_name]),
            'email': userinfo['email'],
            'password': helpers.generate_password(),
            'fullname': userinfo[user_fullname],
            'plugin_extras': {
                'idp': 'sso'
            }
        }
        context = {"model": model, "session": model.Session}
        g.user_obj = helpers.process_user(user_dict)
        g.user = g.user_obj.name
        context['user'] = g.user
        context['auth_user_obj'] = g.user_obj

        # Determine target
        target = None
        if tk.asbool(ckan_config.get('ckanext.keycloak.enable_return_to', False)):
            try:
                target = ckan_session.pop('after_login_url', None)
                #ckan_session.save()
                # no session.save for ckan 2.11, Flask 2.2+
            except Exception as e:
                log.warning("Could not pop after_login_url from session: %r", e)
                target = None

        if not target:
            # Fallback behavior: configurable, defaults to current behavior (user.me)
            fb = ckan_config.get('ckanext.keycloak.return_to_fallback', 'route:user.me')
            if fb.startswith('config:'):
                # eg config:ckan.route_after_login
                key = fb.split(':', 1)[1]
                val = ckan_config.get(key)
                if val and (val.startswith(('http://', 'https://', '/'))):
                    target = val
                else:
                    target = h.url_for(val or 'user.me')
            elif fb.startswith('route:'):
                target = h.url_for(fb.split(':', 1)[1])
            elif fb.startswith(('http://', 'https://', '/', 'url:')):
                target = fb.replace('url:', '', 1)
            else:
                target = h.url_for('user.me')

        response = tk.redirect_to(target)
        _log_user_into_ckan(response)
        log.info("Logged in success")
        return response
    else:
        return tk.redirect_to(tk.url_for('user.login'))


def reset_password():
    email = tk.request.form.get('user', None)
    if '@' not in email:
        log.info(f'User requested reset link for invalid email: {email}')
        h.flash_error('Invalid email address')
        return tk.redirect_to(tk.url_for('user.request_reset'))
    user = model.User.by_email(email)
    if not user:
        log.info(u'User requested reset link for unknown user: {}'.format(email))
        return tk.redirect_to(tk.url_for('user.login'))
    user_extras = user[0].plugin_extras
    if user_extras and user_extras.get('idp', None) == 'sso':
        log.info(u'User requested reset link for sso user: {}'.format(email))
        h.flash_error('Password reset for SSO user not possible')
        return tk.redirect_to(tk.url_for('user.login'))
    return RequestResetView().post()


keycloak.add_url_rule('/sso', view_func=sso)
keycloak.add_url_rule('/sso_login', view_func=sso_login)
keycloak.add_url_rule('/reset_password', view_func=reset_password, methods=['POST'])


def get_blueprint():
    return keycloak
