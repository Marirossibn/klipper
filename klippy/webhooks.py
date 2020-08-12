# Klippy WebHooks registration and server connection
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license
import logging
import socket
import os
import sys
import errno
import json
import homing

# Json decodes strings as unicode types in Python 2.x.  This doesn't
# play well with some parts of Klipper (particuarly displays), so we
# need to create an object hook. This solution borrowed from:
#
# https://stackoverflow.com/questions/956867/
#
def byteify(data, ignore_dicts=False):
    if isinstance(data, unicode):
        return data.encode('utf-8')
    if isinstance(data, list):
        return [byteify(i, True) for i in data]
    if isinstance(data, dict) and not ignore_dicts:
        return {byteify(k, True): byteify(v, True)
                for k, v in data.items()}
    return data

def json_loads_byteified(data):
    return byteify(
        json.loads(data, object_hook=byteify), True)

class WebRequestError(homing.CommandError):
    def __init__(self, message,):
        Exception.__init__(self, message)

    def to_dict(self):
        return {
            'error': 'WebRequestError',
            'message': self.message}

class Sentinel:
    pass

class WebRequest:
    error = WebRequestError
    def __init__(self, client_conn, base_request):
        self.client_conn = client_conn
        self.id = base_request['id']
        self.path = base_request['path']
        self.args = base_request['args']
        self.response = None

    def get_client_connection(self):
        return self.client_conn

    def get(self, item, default=Sentinel):
        if item not in self.args:
            if default == Sentinel:
                raise WebRequestError("Invalid Argument [%s]" % item)
            return default
        return self.args[item]

    def get_int(self, item):
        return int(self.get(item))

    def get_float(self, item):
        return float(self.get(item))

    def get_args(self):
        return self.args

    def get_path(self):
        return self.path

    def set_error(self, error):
        self.response = error.to_dict()

    def send(self, data):
        if self.response is not None:
            raise WebRequestError("Multiple calls to send not allowed")
        self.response = data

    def finish(self):
        if self.response is None:
            # No error was set and the user never executed
            # send, default response is "ok"
            self.response = "ok"
        return {"request_id": self.id, "response": self.response}

class ServerSocket:
    def __init__(self, webhooks, printer):
        self.printer = printer
        self.webhooks = webhooks
        self.reactor = printer.get_reactor()
        self.sock = self.fd_handle = None
        self.clients = {}
        start_args = printer.get_start_args()
        server_address = start_args.get('apiserver')
        is_fileinput = (start_args.get('debuginput') is not None)
        if not server_address or is_fileinput:
            # Do not enable server
            return
        self._remove_socket_file(server_address)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(0)
        self.sock.bind(server_address)
        self.sock.listen(1)
        self.fd_handle = self.reactor.register_fd(
            self.sock.fileno(), self._handle_accept)
        printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)

    def _handle_accept(self, eventtime):
        try:
            sock, addr = self.sock.accept()
        except socket.error:
            return
        sock.setblocking(0)
        client = ClientConnection(self, sock)
        self.clients[client.uid] = client

    def _handle_disconnect(self):
        for client in list(self.clients.values()):
            client.close()
        if self.sock is not None:
            self.reactor.unregister_fd(self.fd_handle)
            try:
                self.sock.close()
            except socket.error:
                pass

    def _remove_socket_file(self, file_path):
        try:
            os.remove(file_path)
        except OSError:
            if os.path.exists(file_path):
                logging.exception(
                    "webhooks: Unable to delete socket file '%s'"
                    % (file_path))
                raise

    def pop_client(self, client_id):
        self.clients.pop(client_id, None)

    def send_all_clients(self, data):
        for client in self.clients.values():
            client.send(data)

class ClientConnection:
    def __init__(self, server, sock):
        self.printer = server.printer
        self.webhooks = server.webhooks
        self.reactor = server.reactor
        self.server = server
        self.uid = id(self)
        self.sock = sock
        self.fd_handle = self.reactor.register_fd(
            self.sock.fileno(), self.process_received)
        self.partial_data = self.send_buffer = ""
        self.is_sending_data = False
        logging.info(
            "webhooks: New connection established")

    def close(self):
        if self.fd_handle is not None:
            logging.info("webhooks: Client connection closed")
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
            try:
                self.sock.close()
            except socket.error:
                pass
            self.server.pop_client(self.uid)

    def is_closed(self):
        return self.fd_handle is None

    def process_received(self, eventtime):
        try:
            data = self.sock.recv(4096)
        except socket.error as e:
            # If bad file descriptor allow connection to be
            # closed by the data check
            if e.errno == errno.EBADF:
                data = ''
            else:
                return
        if data == '':
            # Socket Closed
            self.close()
            return
        requests = data.split('\x03')
        requests[0] = self.partial_data + requests[0]
        self.partial_data = requests.pop()
        for req in requests:
            logging.debug(
                "webhooks: Request received: %s" % (req))
            try:
                web_request = WebRequest(self, json_loads_byteified(req))
            except Exception:
                logging.exception(
                    "webhooks: Error decoding Server Request %s"
                    % (req))
                continue
            self.reactor.register_callback(
                lambda e, s=self, wr=web_request: s._process_request(wr))

    def _process_request(self, web_request):
        try:
            func = self.webhooks.get_callback(
                web_request.get_path())
            func(web_request)
        except homing.CommandError as e:
            web_request.set_error(WebRequestError(e.message))
        except Exception as e:
            msg = "Internal Error on WebRequest: %s" % (web_request.get_path())
            logging.exception(msg)
            web_request.set_error(WebRequestError(e.message))
            self.printer.invoke_shutdown(msg)
        result = web_request.finish()
        logging.debug(
            "webhooks: Sending response - %s" % (str(result)))
        self.send({'method': "response", 'params': result})

    def send(self, data):
        self.send_buffer += json.dumps(data) + "\x03"
        if not self.is_sending_data:
            self.is_sending_data = True
            self.reactor.register_callback(self._do_send)

    def _do_send(self, eventtime):
        retries = 10
        while self.send_buffer:
            try:
                sent = self.sock.send(self.send_buffer)
            except socket.error as e:
                if e.errno == errno.EBADF or e.errno == errno.EPIPE \
                        or not retries:
                    sent = 0
                else:
                    retries -= 1
                    waketime = self.reactor.monotonic() + .001
                    self.reactor.pause(waketime)
                    continue
            retries = 10
            if sent > 0:
                self.send_buffer = self.send_buffer[sent:]
            else:
                logging.info(
                    "webhooks: Error sending server data,  closing socket")
                self.close()
                break
        self.is_sending_data = False

class WebHooks:
    def __init__(self, printer):
        self.printer = printer
        self._endpoints = {"list_endpoints": self._handle_list_endpoints}
        self.register_endpoint("info", self._handle_info_request)
        self.register_endpoint("emergency_stop", self._handle_estop_request)
        self.sconn = ServerSocket(self, printer)

    def register_endpoint(self, path, callback):
        if path in self._endpoints:
            raise WebRequestError("Path already registered to an endpoint")
        self._endpoints[path] = callback

    def _handle_list_endpoints(self, web_request):
        web_request.send({'endpoints': self._endpoints.keys()})

    def _handle_info_request(self, web_request):
        state_message, state = self.printer.get_state_message()
        klipper_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), ".."))
        response = {'state': state, 'state_message': state_message,
                    'hostname': socket.gethostname(),
                    'klipper_path': klipper_path, 'python_path': sys.executable}
        start_args = self.printer.get_start_args()
        for sa in ['log_file', 'config_file', 'software_version', 'cpu_info']:
            response[sa] = start_args.get(sa)
        web_request.send(response)

    def _handle_estop_request(self, web_request):
        self.printer.invoke_shutdown("Shutdown due to webhooks request")

    def get_connection(self):
        return self.sconn

    def get_callback(self, path):
        cb = self._endpoints.get(path, None)
        if cb is None:
            msg = "webhooks: No registered callback for path '%s'" % (path)
            logging.info(msg)
            raise WebRequestError(msg)
        return cb

    def call_remote_method(self, method, **kwargs):
        self.sconn.send_all_clients({'method': method, 'params': kwargs})

    def _action_call_remote_method(self, method, **kwargs):
        self.call_remote_method(method, **kwargs)
        return ""

    def get_status(self, eventtime):
        state_message, state = self.printer.get_state_message()
        return {'state': state, 'state_message': state_message,
                "action_call_remote_method": self._action_call_remote_method}

class GCodeHelper:
    def __init__(self, printer):
        self.printer = printer
        self.gcode = printer.lookup_object("gcode")
        # Output subscription tracking
        self.is_output_registered = False
        self.clients = {}
        # Register webhooks
        wh = printer.lookup_object('webhooks')
        wh.register_endpoint("gcode/help", self._handle_help)
        wh.register_endpoint("gcode/script", self._handle_script)
        wh.register_endpoint("gcode/restart", self._handle_restart)
        wh.register_endpoint("gcode/firmware_restart",
                             self._handle_firmware_restart)
        wh.register_endpoint("gcode/subscribe_output",
                             self._handle_subscribe_output)
    def _handle_help(self, web_request):
        web_request.send(self.gcode.get_command_help())
    def _handle_script(self, web_request):
        self.gcode.run_script(web_request.get('script'))
    def _handle_restart(self, web_request):
        self.gcode.run_script('restart')
    def _handle_firmware_restart(self, web_request):
        self.gcode.run_script('firmware_restart')
    def _output_callback(self, msg):
        for cconn, template in list(self.clients.items()):
            if cconn.is_closed():
                del self.clients[cconn]
                continue
            tmp = dict(template)
            tmp['params'] = {'response': msg}
            cconn.send(tmp)
    def _handle_subscribe_output(self, web_request):
        cconn = web_request.get_client_connection()
        template = web_request.get('response_template', {})
        self.clients[cconn] = template
        if not self.is_output_registered:
            self.gcode.register_output_handler(self._output_callback)
            self.is_output_registered = True

SUBSCRIPTION_REFRESH_TIME = .25

class QueryStatusHelper:
    def __init__(self, printer):
        self.printer = printer
        self.clients = {}
        self.pending_queries = []
        self.query_timer = None
        self.last_query = {}
        # Register webhooks
        webhooks = printer.lookup_object('webhooks')
        webhooks.register_endpoint("objects/list", self._handle_list)
        webhooks.register_endpoint("objects/query", self._handle_query)
        webhooks.register_endpoint("objects/subscribe", self._handle_subscribe)
    def _handle_list(self, web_request):
        objects = [n for n, o in self.printer.lookup_objects()
                   if hasattr(o, 'get_status')]
        web_request.send({'objects': objects})
    def _do_query(self, eventtime):
        last_query = self.last_query
        query = self.last_query = {}
        msglist = self.pending_queries
        self.pending_queries = []
        msglist.extend(self.clients.values())
        # Generate get_status() info for each client
        for cconn, subscription, send_func, template in msglist:
            is_query = cconn is None
            if not is_query and cconn.is_closed():
                del self.clients[cconn]
                continue
            # Query each requested printer object
            cquery = {}
            for obj_name, req_items in subscription.items():
                res = query.get(obj_name, None)
                if res is None:
                    po = self.printer.lookup_object(obj_name, None)
                    if po is None or not hasattr(po, 'get_status'):
                        res = query[obj_name] = {}
                    else:
                        res = query[obj_name] = po.get_status(eventtime)
                if req_items is None:
                    req_items = list(res.keys())
                    if req_items:
                        subscription[obj_name] = req_items
                lres = last_query.get(obj_name, {})
                cres = {}
                for ri in req_items:
                    rd = res.get(ri, None)
                    if not callable(rd) and (is_query or rd != lres.get(ri)):
                        cres[ri] = rd
                if cres or is_query:
                    cquery[obj_name] = cres
            # Send data
            if cquery or is_query:
                tmp = dict(template)
                tmp['params'] = {'eventtime': eventtime, 'status': cquery}
                send_func(tmp)
        if not query:
            # Unregister timer if there are no longer any subscriptions
            reactor = self.printer.get_reactor()
            reactor.unregister_timer(self.query_timer)
            self.query_timer = None
            return reactor.NEVER
        return eventtime + SUBSCRIPTION_REFRESH_TIME
    def _handle_query(self, web_request, is_subscribe=False):
        objects = web_request.get('objects')
        # Validate subscription format
        if type(objects) != type({}):
            raise web_request.error("Invalid argument")
        for k, v in objects.items():
            if type(k) != type("") or type(v) not in [type([]), type(None)]:
                raise web_request.error("Invalid argument")
            if v is not None:
                for ri in v:
                    if type(ri) != type(""):
                        raise web_request.error("Invalid argument")
        # Add to pending queries
        cconn = web_request.get_client_connection()
        template = web_request.get('response_template', {})
        if is_subscribe and cconn in self.clients:
            del self.clients[cconn]
        reactor = self.printer.get_reactor()
        complete = reactor.completion()
        self.pending_queries.append((None, objects, complete.complete, {}))
        # Start timer if needed
        if self.query_timer is None:
            qt = reactor.register_timer(self._do_query, reactor.NOW)
            self.query_timer = qt
        # Wait for data to be queried
        msg = complete.wait()
        web_request.send(msg['params'])
        if is_subscribe:
            self.clients[cconn] = (cconn, objects, cconn.send, template)
    def _handle_subscribe(self, web_request):
        self._handle_query(web_request, is_subscribe=True)

def add_early_printer_objects(printer):
    printer.add_object('webhooks', WebHooks(printer))
    GCodeHelper(printer)
    QueryStatusHelper(printer)
