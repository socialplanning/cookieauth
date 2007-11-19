# Copyright (C) 2006-2007 The Open Planning Project

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the 
# Free Software Foundation, Inc., 
# 51 Franklin Street, Fifth Floor, 
# Boston, MA  02110-1301
# USA

from paste.wsgilib import intercept_output
from paste.request import construct_url

import os
import base64

import hmac
import sha

from urllib import quote, unquote, urlencode
from Cookie import BaseCookie

def _user_dict(name):
    return dict(username = name,
                email = "%s@topp.example.com" % name, 
                roles = 'Authenticated ProjectMember'.split(),
                )

import usermapper

import httplib2
import elementtree.ElementTree as ET

from topp.utils import memorycache

def admin_post(url, admin_info):
    h = httplib2.Http()
    # because of some zope silliness we have to do this as a POST instead of basic auth
    data = {"__ac_name":admin_info[0], "__ac_password":admin_info[1]}
    body = urlencode(data)
    resp, content = h.request(url, method="POST", body=body, redirections=0)
    return resp, content

class ProjectNotFoundError(Exception): pass

@memorycache.cache(120)
def get_users_for_project(project, server, admin_info):
    resp, content = admin_post("%s/projects/%s/members.xml" % (server, project), admin_info)
    
    #404 means the project isn't fully initialized.
    if resp['status'] == '404':
        raise ProjectNotFoundError

    if resp['status'] != '200':
        if resp['status'] == '302':
            # redirect probably means auth failed
            extra = '; did your admin authentication fail?'
        if resp['status'] == '400':
            # Probably Zope is gone
            extra = '; is Zope started?'
        else:
            extra = ''
            
        raise ValueError("Error retrieving project %s: status %s%s" 
                         % (project, resp['status'], extra))
    tree = ET.fromstring(content)
    members = []
    for member in tree:
        m = {}
        m['username'] = member.find('id').text
        m['roles'] = []
        for role in member.findall('role'):
            m['roles'].append(role.text)
        members.append(m)
    return members

@memorycache.cache(120)
def get_info_for_project(project, server, admin_info):
    resp, content = admin_post("%s/projects/%s/info.xml" % (server, project), admin_info)
#    h = httplib2.Http()
#    resp, content = h.request("%s/projects/%s/info.xml" % (server, project), "GET")
    if resp['status'] == '404':
        raise ProjectNotFoundError #don't let this be cached
    if resp['status'] != '200':
        raise ValueError("Error retrieving project %s: status %s" % (project, resp['status']))
    tree = ET.fromstring(content)
    policy = tree[0]
    assert policy.tag == "policy", ("Bad info from project info getter")
    info = dict(policy=policy.text)
    return info

def get_secret(conf):
    secret_filename = conf['topp_secret_filename']
    f = open(secret_filename)
    secret = f.readline().strip()
    f.close()
    return secret

class UserMapper(usermapper.UserMapper):

    def __init__(self, environ, project, server, admin_info, profile_uri):
        usermapper.UserMapper.__init__(self)
        self.project = project
        self.server = server
        self.environ = environ
	self.admin_info = admin_info
        self.profile_uri = profile_uri

    def member_url(self, name):
        return self.profile_uri % name

    def project_members(self):
        try:
            return get_users_for_project(self.project, self.server, self.admin_info)
        except ProjectNotFoundError:  # assume no members
            return []

    def is_project_member(self, member):
        return member in self.project_member_names()
    
class BadCookieError(Exception): pass

class CookieAuth(object):
    def __init__(self, app, app_conf):
        self.app = app
        self.openplans_instance = app_conf['openplans_instance']
        self.login_uri = app_conf['login_uri']
        self.homepage_uri = app_conf['homepage_uri']
        self.profile_uri = app_conf['profile_uri']

        if self.profile_uri.count('%s') != 1:
            raise Exception("Badly formatted profile_uri: must include a single '%s'")

        admin_file = app_conf['topp_admin_info_filename']
        self.admin_info = tuple(file(admin_file).read().strip().split(":"))
        if len(self.admin_info) != 2:
            raise Exception("Bad format in administrator info file")

        self.secret = get_secret(app_conf)

    def authenticate(self, environ):
        username = environ.get('REMOTE_USER')
        if username:
            environ['topp.user_info'] = dict(username = username, 
                                             roles = ['Authenticated'],
                                             email = '%s@example.com' % username)
            return True
        
        try:
            cookie = BaseCookie(environ['HTTP_COOKIE'])
            morsel = cookie['__ac']
        except KeyError:
            return False

        try:
            username, auth = base64.decodestring(unquote(morsel.value)).split("\0")
        except ValueError:
            raise BadCookieError
            
        if not auth == hmac.new(self.secret, username, sha).hexdigest():
            return False

        environ['REMOTE_USER'] = username
        environ['topp.user_info'] = dict(username = username, 
                                         roles = ['Authenticated'],
                                         email = '%s@example.com' % username)
        return True
        
    def needs_redirection(self, status, headers):
        return status.startswith('401') or status.startswith('403')

    def __call__(self, environ, start_response):
        if environ['PATH_INFO'].strip("/").startswith("_debug"):
            return self.app(environ, start_response)
        
	username = ''
        environ['topp.user_info'] = dict(username = '', roles = ['Anonymous'], email = 'null@example.com')
	try:
	    authenticated = self.authenticate(environ)
        except BadCookieError:
            status = "401 Unauthorized"
            start_response(status, [])
            return ["Please delete your brower's cookies and login again."]

        if authenticated:
            username = environ['REMOTE_USER']

        if 'topp.project_name' in environ:
            project_name = environ['topp.project_name']

            environ['topp.project_members'] = umapper = UserMapper(environ, project_name,
                                                                   self.openplans_instance,
                                                                   self.admin_info, self.profile_uri)
            if username in umapper.project_member_names():
                environ['topp.user_info']['roles'].extend(umapper.project_member_roles(username))

            try:
                environ['topp.project_permission_level'] = get_info_for_project(project_name, self.openplans_instance, self.admin_info)['policy']
            except ProjectNotFoundError: #assume the most restrictive
                environ['topp.project_permission_level'] = dict(policy='closed_policy')

        status, headers, body = intercept_output(environ, self.app, self.needs_redirection, start_response)

        if status:
            if status.startswith('401'):
                status = "303 See Other"
                url = construct_url(environ)
                headers = [('Location', '%s?came_from=%s' % (self.login_uri, quote(url))), ('Content-Type', 'text/html')]
                start_response(status, headers)
                return []
            elif status.startswith('403'):
                status = "303 See Other"
                url = construct_url(environ)
                headers = [('Location', '%s?portal_status_message=You+have+insufficient+privileges.' % self.homepage_uri)]
                start_response(status, headers)
            return []        
        else:
            return body

def make_cookie(username):
    from pylons import config
    secret = get_secret(config['app_conf'])
    auth = hmac.new(secret, username, sha).hexdigest()
    cookie = quote(("%s\0%s" % (username, auth)).encode("base64")).strip()
    return ('__ac', cookie)
