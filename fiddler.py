#!/usr/bin/env python
# -*- coding: utf-8 -*-

import tornado
import tornado.web
import tornado.ioloop
import tornado.gen
import tornado.httpclient
import tornado.httputil
import tornado.websocket
import tornado.options

from tornado.options import define, options

import json
import urlparse
from uuid import uuid4
from pdb import set_trace as st
from collections import OrderedDict as odict


define('proxy', default=8888, type=int, help='run proxy server on this port')
define('console', default=8889, type=int, help='run console server on this port')


#RequestPool = {}

class RequestPoolObject(dict):

    def __init__(self,*args,**kwargs):
        self.update(self, *args, **kwargs)
        self.callback = None

    def _notify(self,key,value):
        if self.callback:
            self.callback(key,value)

RequestPool = RequestPoolObject()

class configure(object):

    def __init__(self):
        self.BlockRequest = False
        self.BlockResponse = False
        self.StreamResponse = False

conf = configure()

def dumpdefault(s):
    if isinstance(s, ReqInfo): return s.dict() 
    if not type(s) in (dict, list, tuple, int, float, str, odict):  return repr(s)
    return s


class ReqInfo:

    idenity = 0

    def __init__(self, uuid, handler, block_on_response = 1):
        self.uuid = uuid
        self.handler = handler
        self.blockType = ('response','request','released')[block_on_response]
        self.id = self.newid()

    @classmethod
    def newid(cls):
        ReqInfo.idenity+=1
        return ReqInfo.idenity

    @property
    def _get_scheme(self):
        return urlparse.urlparse(self.handler.request.uri, scheme='http').scheme

    @property
    def _get_body_len(self):
        if self.blockType == 'request': return 0
        if self.blockType in ('response','released'):
            if self.handler.response.body: 
                return len(self.handler.response.body)
            else:
                return 0

    @property
    def _get_ctype(self):
        if self.blockType == 'request': return None
        if self.blockType in ('response', 'released'):
           return self.handler.response.headers.get('Content-Type',None)


    def dict(self,extr=False):
        baseinfo = dict(
                id=self.id, 
                code=self.handler.response.code, method=self.handler.request.method, protocol=self._get_scheme, 
                url=self.handler.request.uri, bodylen=self._get_body_len,
                cType=self._get_ctype, Type=self.blockType
                )
        if not extr: return baseinfo
        baseinfo['req']={}
        baseinfo['res']={}
        baseinfo['req']['headers'] = [i for i in self.handler.request.headers.get_all()]
        baseinfo['req']['body'] = self.handler.request.body
        if self.blockType in ('response','released'):
            baseinfo['res']['headers'] = [i for i in self.handler.response.headers.get_all()]
            baseinfo['res']['body'] = self.handler.response.body
            baseinfo['res']['reason'] = self.handler.response.reason
        return baseinfo
        


    def __repr__(self):
        return json.dumps(self.dict())

    def block(self):
        RequestPool[self.uuid]=self
        RequestPool._notify(self.uuid, self.dict())
        return self

    def release(self):
        if self.blockType == 'request':
            self.handler.start_request()
            return self
        elif self.blockType == 'response':
            self.handler.start_response()
            RequestPool._notify(self.uuid, self.dict())
            return self
        elif self.blockType == 'released':
            pass


class Proxy(tornado.web.RequestHandler):

    SUPPORTED_METHODS = tornado.web.RequestHandler.SUPPORTED_METHODS + ('CONNECT',)

    def __init__(self, *args, **kwargs):
        super(Proxy, self).__init__(*args,**kwargs)
        self.uuid = uuid4().hex
        self._responsed = False
        self.response = self
        self.response.code = 0
        self.stream_response_stage = 0
        self.Req = None


    def compute_etag(self):
        return None

    @property
    def conf(self):
        return self.settings.get('c')

    @tornado.web.asynchronous
    def get(self):
        if self.conf.BlockRequest:
            self.Req = ReqInfo(self.uuid, self).block()
        else:
            self.Req = ReqInfo(self.uuid, self).block().release()


    @tornado.web.asynchronous
    def post(self):
        return self.get()


    @tornado.web.asynchronous
    def connect(self):
        return self.get()
        #client = self.request.connection.stream
        #s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        #upstream = tornado.iostream.IOStream(s)
        #read_client = lambda x:upstream.write(x)
        #read_upstream = lambda x:client.write(x)


    def start_response(self):
        if self.response:
            self.set_status(self.response.code, self.response.reason)
            self._headers = tornado.httputil.HTTPHeaders()
            for k, v in self.response.headers.get_all():
                if k not in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding', 'Connection'):
                    self.add_header(k,v)
            if self.response.body:
                self.add_header('Content-Length',len(self.response.body))
                self.write(self.response.body)
            self.finish()
            return


    def stream_response(self,body):
        if self.stream_response_stage == 2:
            self.write(body)

    def hdr_callback(self,header):
        if not self.stream_response_stage:
            http_version, code, reason = header.split(None,2)
            self.set_status(code, reason)
            self.stream_response_stage = 1
        elif header == '\r\n':
            self.stream_response_stage = 2
        else:
            k,v = header.split(':',1)
            self._headers.add(k,v)


    @tornado.gen.coroutine
    def start_request(self):
        if self.request.headers.get('Proxy-Connection'): del self.request.headers['Proxy-Connection']
        if not self.request.body: self.request.body = None

        
        streaming_callback = None
        header_callback = None
        if not self.conf.BlockResponse and self.conf.StreamResponse:
            streaming_callback = self.stream_response
            header_callback = self.hdr_callback

        Fetcher = tornado.httpclient.AsyncHTTPClient()
        self.request.headers['cache-control']='no-cache'
        self.request.headers['Pragma']='no-cache'
        self.response = yield Fetcher.fetch(
                self.request.uri, method=self.request.method,
                headers = self.request.headers, follow_redirects = False,
                allow_nonstandard_methods = True, validate_cert = False,
                streaming_callback = streaming_callback, header_callback = header_callback,
                raise_error = False, body = self.request.body
                )
        print self.response.__dict__

        if self.conf.BlockResponse:
            RequestPool[self.uuid].blockType = 'response'
            RequestPool._notify(self.uuid, self.Req.dict())
        else:
            RequestPool[self.uuid].blockType = 'released'
            self.start_response()
            RequestPool._notify(self.uuid, self.Req.dict())
            
            

class WebServer(tornado.web.RequestHandler):

    @property
    def conf(self):
        return self.settings.get('c')
    
    @tornado.web.asynchronous
    def get(self,rtype):
        if rtype == '':
            #self.finish(open('main.html').read())
            self.render('main.html')

        elif rtype == 'show':
            self.set_header('content-type','application/x-javascript')
            reqpool = odict(sorted(RequestPool.items(), key=lambda x:x[1].id))
            self.finish(json.dumps(reqpool, default=dumpdefault))


        elif rtype == 'detail':
            uuid = self.get_argument('uuid')
            Req = RequestPool.get(uuid,None)
            if Req:
                self.finish(Req.dict(extr=True))

        elif rtype == 'ctrl':
            brq = self.get_argument('brq','off')
            brs = self.get_argument('brs','off')
            self.conf.BlockRequest = bool(brq=='on')
            self.conf.BlockResponse = bool(brs=='on')
            self.finish(dict(brq=self.conf.BlockRequest,brs=self.conf.BlockResponse))
            return

        elif rtype == 'releaseall':
            for r in RequestPool:
                if RequestPool[r].blockType!='released': RequestPool[r].release()

        elif rtype == 'clearlog':
            RequestPool.clear()
            self.finish('done')
            return

    @tornado.web.asynchronous
    def post(self,rtype):
        if rtype == 'release':
            uuid = self.get_argument('uuid')
            if not RequestPool.get(uuid,None): return
            rlst = self.get_argument('rlst')
            if rlst == 'request':
                save_req_Info(self, RequestPool[uuid].handler)
            elif rlst == 'response':
                save_res_Info(self, RequestPool[uuid].handler)
            RequestPool[uuid].release()
            self.finish('done')
            return

def save_res_Info(wb, cls):
    code = wb.get_argument('code',400)
    reason = wb.get_argument('reason','Bad Request')
    headers = wb.get_argument('headers','')
    body = wb.get_argument('body',None)
    header_tuple = map(lambda x:x.split(':',1),headers.strip().split('\n'))
    cls.response.code = int(code)
    cls.response.reason = reason
    for header in header_tuple:
        try:
            h,v = header
            cls.response.headers[h]=v
        except:
            pass
    if body: cls.response._body = body

def save_req_Info(wb, cls):
    method = wb.get_argument('method','GET')
    url = wb.get_argument('url',cls.request.uri)
    headers = wb.get_argument('headers','')
    body = wb.get_argument('body',None)
    header_tuple = map(lambda x:x.split(':',1),headers.strip().split('\n'))
    cls.request.uri = url
    cls.request.method = method
    for header in header_tuple:
        try:
            h,v = header
            cls.request.headers[h]=v
        except:
            pass
    if body: cls.request.body = body
    





class WSHandler(tornado.websocket.WebSocketHandler):

    def open(self):
        RequestPool.callback = self.on_event

    def on_event(self, uuid, obj):
        try:
            self.write_message(json.dumps((uuid,obj)))
        except Exception,e:
            pass


def run():
    tornado.options.parse_command_line()
    
    proxy = tornado.web.Application([
        (r'.*',Proxy)
        ],**dict(c=conf))

    webapp = tornado.web.Application([
        (r'/ws',WSHandler),
        (r'/(.*)',WebServer)
        ],**dict(c=conf,debug=True))

    proxy.listen(options.proxy)
    webapp.listen(options.console)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == '__main__':
    run()
