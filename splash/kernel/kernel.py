# -*- coding: utf-8 -*-
from __future__ import absolute_import
import json
import sys
import time
from IPython.utils import py3compat
from IPython.utils.jsonutil import json_clean

import lupa
from IPython.kernel.zmq.kernelapp import IPKernelApp
from IPython.kernel.zmq.eventloops import loop_qt4
from twisted.internet import defer

import splash
from splash.lua import lua2python, get_version, get_main_sandboxed, get_main
from splash.browser_tab import BrowserTab
from splash.lua_runtime import SplashLuaRuntime
from splash.qtrender_lua import Splash, SplashScriptRunner
from splash.qtutils import init_qt_app
from splash.render_options import RenderOptions
from splash import network_manager
from splash import defaults
from splash.kernel.kernelbase import Kernel
from splash.utils import BinaryCapsule


def init_browser():
    # TODO: support the same command-line options as HTTP server.
    manager = network_manager.create_default()
    proxy_factory = None  # TODO

    data = {}
    data['uid'] = id(data)

    tab = BrowserTab(
        network_manager=manager,
        splash_proxy_factory=proxy_factory,
        verbosity=2,  # TODO
        render_options=RenderOptions(data, defaults.MAX_TIMEOUT),  # TODO: timeout
        visible=True,
    )
    return tab


class DeferredSplashRunner(object):

    def __init__(self, lua, splash, sandboxed, log=None, render_options=None):
        self.lua = lua
        self.splash = splash
        self.sandboxed = sandboxed

        if log is None:
            self.log = self.splash.tab.logger.log
        else:
            self.log = log

        self.runner = SplashScriptRunner(
            lua=self.lua,
            log=self.log,
            splash=splash,
            sandboxed=self.sandboxed,
        )
        self.splash.init_dispatcher(self.runner.dispatch)

    def run(self, main_coro):
        """
        Run main_coro Lua coroutine, passing it a Splash
        instance as an argument. Return a Deferred.
        """
        d = defer.Deferred()

        def return_result(result):
            d.callback(result)

        def return_error(err):
            d.errback(err)

        self.runner.start(
            main_coro=main_coro,
            return_result=return_result,
            return_error=return_error,
        )
        return d


class SplashKernel(Kernel):
    implementation = 'Splash'
    implementation_version = splash.__version__
    language = 'Lua'
    language_version = get_version()
    language_info = {
        'name': 'Splash',
        'mimetype': 'application/x-lua',
        'display_name': 'Splash',
        'language': 'lua',
        'codemirror_mode': 'Lua',
        'file_extension': '.lua',
        'pygments_lexer': 'lua'
    }
    banner = "Splash kernel - write browser automation scripts interactively"
    help_links = [
        {
            'text': "Splash Help",
            'url': 'http://splash.readthedocs.org/en/latest/scripting-ref.html'
        },
    ]

    sandboxed = False

    def __init__(self, **kwargs):
        super(SplashKernel, self).__init__(**kwargs)
        self.tab = init_browser()

        self.lua = SplashLuaRuntime(self.sandboxed, "", ())
        self.lua_repr = self.lua.eval("require('repr')")
        self.splash = Splash(lua=self.lua, tab=self.tab)
        self.runner = DeferredSplashRunner(self.lua, self.splash, self.sandboxed)
        # try:
        #     sys.stdout.write = self._print
        # except:
        #     pass # Can't change stdout

    def log_msg(self, text, min_level=2):
        self._print(text + "\n")

    def _print(self, message):
        stream_content = {'name': 'stdout', 'text': message, 'metadata': dict()}
        self.log.debug('Write: %s' % message)
        self.send_response(self.iopub_socket, 'stream', stream_content)

    def get_main(self, lua_source):
        if self.sandboxed:
            main, env = get_main_sandboxed(self.lua, lua_source)
        else:
            main, env = get_main(self.lua, lua_source)
        return self.lua.create_coroutine(main)

    def _publish_execute_result(self, parent, data, metadata, execution_count):
        msg = {
            u'data': data,
            u'metadata': metadata,
            u'execution_count': execution_count
        }
        self.session.send(self.iopub_socket, u'execute_result', msg,
                          parent=parent, ident=self._topic('execute_result')
        )

    def send_execute_reply(self, stream, ident, parent, md, reply_content):
        def done(result):
            reply, result, ct = result
            # self._print(str(reply))
            # self._print(repr(result)+"\n")
            # self._print(ct)
            super(SplashKernel, self).send_execute_reply(stream, ident, parent, md, reply)

            if result:
                # if not isinstance(result, (str, unicode)):
                #     result = self.lua_repr(self.lua.python2lua(result))
                data = {'text/plain': result}
                # if isinstance(result, BinaryCapsule):
                #     data["image/png"] = result.data
                self._publish_execute_result(parent, data, {}, self.execution_count)

        assert isinstance(reply_content, defer.Deferred)
        reply_content.addCallback(done)

    def do_execute(self, code, silent, store_history=True, user_expressions=None,
                   allow_stdin=False):

        def success(result):
            result, content_type = result
            reply = {
                'status': 'ok',
                'execution_count': self.execution_count,
                'payload': [],
                'user_expressions': {},
            }
            return reply, result, content_type or 'text/plain'

        def error(failure):
            text = str(failure)
            reply = {
                'status': 'error',
                'execution_count': self.execution_count,
                'ename': '',
                'evalue': text,
                'traceback': []
            }
            return reply, failure, 'text/plain'

        try:
            try:
                lua_source = """
                local repr = require("repr")
                function main(splash)
                    return repr(%s)
                end
                """ % code
                main_coro = self.get_main(lua_source)
            except lupa.LuaSyntaxError:
                lua_source = """
                function main(splash)
                    %s
                end
                """ % code
                main_coro = self.get_main(lua_source)
        except Exception:
            d = defer.Deferred()
            d.addCallbacks(success, error)
            d.errback()
            return d

        d = self.runner.run(main_coro)
        d.addCallbacks(success, error)
        return d


def start():
    # FIXME: logs go to nowhere
    init_qt_app(verbose=False)
    kernel = IPKernelApp.instance(kernel_class=SplashKernel)
    kernel.initialize()
    kernel.kernel.eventloop = loop_qt4
    kernel.start()
