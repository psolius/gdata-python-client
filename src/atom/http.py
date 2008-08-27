#!/usr/bin/python
#
# Copyright (C) 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""HttpClients in this module use httplib to make HTTP requests.

This module make HTTP requests based on httplib, but there are environments
in which an httplib based approach will not work (if running in Google App
Engine for example). In those cases, higher level classes (like AtomService
and GDataService) can swap out the HttpClient to transparently use a 
different mechanism for making HTTP requests.

  HttpClient: Contains a request method which performs an HTTP call to the 
      server.
      
  ProxiedHttpClient: Contains a request method which connects to a proxy using
      settings stored in operating system environment variables then 
      performs an HTTP call to the endpoint server.
"""


__author__ = 'api.jscudder (Jeff Scudder)'


import types
import os
import httplib
import atom.url
import atom.http_interface
import socket
import base64

class ProxyError(atom.http_interface.Error):
  pass


DEFAULT_CONTENT_TYPE = 'application/atom+xml'


class HttpClient(object):
  def __init__(self, headers=None):
    self.debug = False
    self.headers = headers or {}

  def request(self, operation, url, data=None, headers=None):
    """Performs an HTTP call to the server, supports GET, POST, PUT, and 
    DELETE.

    Usage example, perform and HTTP GET on http://www.google.com/:
      import atom.http
      client = atom.http.HttpClient()
      http_response = client.request('GET', 'http://www.google.com/')

    Args:
      operation: str The HTTP operation to be performed. This is usually one
          of 'GET', 'POST', 'PUT', or 'DELETE'
      data: filestream, list of parts, or other object which can be converted
          to a string. Should be set to None when performing a GET or DELETE.
          If data is a file-like object which can be read, this method will 
          read a chunk of 100K bytes at a time and send them. 
          If the data is a list of parts to be sent, each part will be 
          evaluated and sent.
      url: The full URL to which the request should be sent. Can be a string
          or atom.url.Url.
      headers: dict of strings. HTTP headers which should be sent
          in the request. 
    """
    if not isinstance(url, atom.url.Url):
      if isinstance(url, types.StringType):
        url = atom.url.parse_url(url)
      else:
        raise atom.http_interface.UnparsableUrlObject('Unable to parse url '
            'parameter because it was not a string or atom.url.Url')
    
    all_headers = self.headers.copy()
    if headers:
      all_headers.update(headers) 

    connection = self._prepare_connection(url, all_headers)

    if self.debug:
      connection.debuglevel = 1

    connection.putrequest(operation, self._get_access_url(url))

    # If the list of headers does not include a Content-Length, attempt to
    # calculate it based on the data object.
    if data and 'Content-Length' not in all_headers:
      if isinstance(data, types.StringType):
        all_headers['Content-Length'] = len(data)
      else:
        raise atom.http_interface.ContentLengthRequired('Unable to calculate '
            'the length of the data parameter. Specify a value for '
            'Content-Length')

    # Set the content type to the default value if none was set.
    if 'Content-Type' not in all_headers:
      all_headers['Content-Type'] = DEFAULT_CONTENT_TYPE

    # Send the HTTP headers.
    for header_name in all_headers:
      connection.putheader(header_name, all_headers[header_name])
    connection.endheaders()

    # If there is data, send it in the request.
    if data:
      if isinstance(data, list):
        for data_part in data:
          _send_data_part(data_part, connection)
      else:
        _send_data_part(data, connection)

    # Return the HTTP Response from the server.
    return connection.getresponse()
    
  def _prepare_connection(self, url, headers):
    if not isinstance(url, atom.url.Url):
      if isinstance(url, types.StringType):
        url = atom.url.parse_url(url)
      else:
        raise atom.http_interface.UnparsableUrlObject('Unable to parse url '
            'parameter because it was not a string or atom.url.Url')
    if url.protocol == 'https':
      if not url.port:
        url.port = 443
      return httplib.HTTPSConnection(url.host, url.port)
    else:
      if not url.port:
        url.port = 80
      return httplib.HTTPConnection(url.host, url.port)

  def _get_access_url(self, url):
    return url.get_request_uri()


class ProxiedHttpClient(HttpClient):
  """Performs an HTTP request through a proxy.
  
  The proxy settings are obtained from enviroment variables. The URL of the 
  proxy server is assumed to be stored in the environment variables 
  'https_proxy' and 'http_proxy' respectively. If the proxy server requires
  a Basic Auth authorization header, the username and password are expected to 
  be in the 'proxy-username' or 'proxy_username' variable and the 
  'proxy-password' or 'proxy_password' variable.
  
  After connecting to the proxy server, the request is completed as in 
  HttpClient.request.
  """
        
  def _prepare_connection(self, url, headers):
    proxy_auth = _get_proxy_auth()
    if url.protocol == 'https':
      # destination is https
      proxy = os.environ.get('https_proxy')
      if proxy:
        # Set any proxy auth headers 
        if proxy_auth:
          proxy_auth = 'Proxy-authorization: %s' % proxy_auth
          
        # Construct the proxy connect command.
        port = url.port
        if not port:
          port = '443'
        proxy_connect = 'CONNECT %s:%s HTTP/1.0\r\n' % (url.host, port)
        
        # Set the user agent to send to the proxy
        if headers and 'User-Agent' in headers:
          user_agent = 'User-Agent: %s\r\n' % (headers['User-Agent'])
        else:
          user_agent = ''
        
        proxy_pieces = '%s%s%s\r\n' % (proxy_connect, proxy_auth, user_agent)
        
        # Find the proxy host and port.
        proxy_url = atom.url.parse_url(proxy)
        if not proxy_url.port:
          proxy_url.port = '80'
        
        # Connect to the proxy server, very simple recv and error checking
        p_sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        p_sock.connect((proxy_url.host, int(proxy_url.port)))
        p_sock.sendall(proxy_pieces)
        response = ''

        # Wait for the full response.
        while response.find("\r\n\r\n") == -1:
          response += p_sock.recv(8192)
       
        p_status = response.split()[1]
        if p_status != str(200):
          raise ProxyError('Error status=%s' % str(p_status))

        # Trivial setup for ssl socket.
        ssl = socket.ssl(p_sock, None, None)
        fake_sock = httplib.FakeSocket(p_sock, ssl)
 
        # Initalize httplib and replace with the proxy socket.
        connection = httplib.HTTPConnection(proxy_url.host)
        connection.sock=fake_sock
        return connection
      else:
        # The request was HTTPS, but there was no https_proxy set.
        return HttpClient._prepare_connection(self, url, headers)
    else:
      proxy = os.environ.get('http_proxy')
      if proxy:
        # Find the proxy host and port.
        proxy_url = atom.url.parse_url(proxy)
        if not proxy_url.port:
          proxy_url.port = '80'
        
        if proxy_auth:
          headers['Proxy-Authorization'] = proxy_auth.strip()

        return httplib.HTTPConnection(proxy_url.host, proxy_url.port)
      else:
        # The request was HTTP, but there was no http_proxy set.
        return HttpClient._prepare_connection(self, url, headers)

  def _get_access_url(self, url):
    proxy = os.environ.get('http_proxy')
    if url.protocol == 'http' and proxy:
      return url.to_string()
    else:
      return url.get_request_uri()


def _get_proxy_auth():
  proxy_username = os.environ.get('proxy-username')
  if not proxy_username:
    proxy_username = os.environ.get('proxy_username')
  proxy_password = os.environ.get('proxy-password')
  if not proxy_password:
    proxy_password = os.environ.get('proxy_password')
  if proxy_username:
    user_auth = base64.encodestring('%s:%s' % (proxy_username,
                                               proxy_password))
    return 'Basic %s\r\n' % (user_auth.strip())
  else:
    return ''


def _send_data_part(data, connection):
  if isinstance(data, types.StringType):
    connection.send(data)
    return
  # Check to see if data is a file-like object that has a read method.
  elif hasattr(data, 'read'):
    # Read the file and send it a chunk at a time.
    while 1:
      binarydata = data.read(100000)
      if binarydata == '': break
      connection.send(binarydata)
    return
  else:
    # The data object was not a file.
    # Try to convert to a string and send the data.
    connection.send(str(data))
    return