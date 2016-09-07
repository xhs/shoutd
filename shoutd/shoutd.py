#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from raftkit import RaftUDPAgent
from pyroute2 import IPRoute, NetNS, netns
import requests
import asyncio
from urllib.parse import urlparse, parse_qsl
import json
import hashlib
import time
import os
import re
import traceback

import structlog
structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())
logger = structlog.get_logger('shoutd')

SHOUTD_PLUGIN_DIR = '/usr/lib/docker/plugins'
SHOUTD_SPEC_FILE = 'shoutd.spec'
SHOUTD_PORT = int(os.environ.get('SHOUTD_PORT', 7788))

length_regex = re.compile(r'Content-Length: ([0-9]+)\r\n', re.IGNORECASE)


class HttpRequest(object):
    def __init__(self, method, url, headers, payload=None):
        print(100, method, url, headers, payload)
        self.method = method
        p = urlparse(url)
        self.path = p.path
        self.params = dict(parse_qsl(p.query))
        self.headers = headers
        if payload:
            self.payload = json.loads(payload.decode('utf-8'))
        else:
            self.payload = {}

    def __str__(self):
        return '<HttpRequest method=%s, path=%s, params=%s, headers=%s, payload=%s>' \
               % (self.method, self.path, self.params, self.headers, self.payload)

    def __getattr__(self, name):
        return self.payload.get(name)

    def __getitem__(self, key):
        return self.payload.get(key)


class HttpResponse(object):
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload
        self._map = {
            200: '200 OK',
            403: '403 Forbidden',
            404: '404 Not Found',
            405: '405 Method Not Allowed',
            500: '500 Internal Server Error'
        }

    def __str__(self):
        return '<HttpResponse status=%d, payload=%s>' \
               % (self.status, self.payload)

    @property
    def data(self):
        b = ('HTTP/1.1 %s\r\n\r\n' % self._map[self.status]).encode('utf-8')
        if self.payload is not None:
            b += json.dumps(self.payload).encode('utf-8')
        return b


class ShoutD(object):
    def __init__(self, advertise_address, peer_addresses=None, loop=None):
        if loop:
            self._loop = loop
        else:
            self._loop = asyncio.get_event_loop()
        self._local_address = advertise_address.split(':')[0]
        self._raft = RaftUDPAgent(advertise_address, peer_addresses=peer_addresses, event_loop=self._loop)
        self._routes = {}

        self._r('/Plugin.Activate', 'POST', self._handshake)
        self._r('/NetworkDriver.GetCapabilities', 'POST', self._get_capabilities)
        self._r('/NetworkDriver.CreateNetwork', 'POST', self._create_network)
        self._r('/NetworkDriver.DeleteNetwork', 'POST', self._delete_network)
        self._r('/NetworkDriver.CreateEndpoint', 'POST', self._create_endpoint)
        self._r('/NetworkDriver.EndpointOperInfo', 'POST', self._get_endpoint_info)
        self._r('/NetworkDriver.DeleteEndpoint', 'POST', self._delete_endpoint)
        self._r('/NetworkDriver.Join', 'POST', self._join)
        self._r('/NetworkDriver.Leave', 'POST', self._leave)
        self._r('/NetworkDriver.DiscoverNew', 'POST', self._new_discovery)
        self._r('/NetworkDriver.DiscoverDelete', 'POST', self._delete_discovery)

        self._r('/actions/delete-network', 'POST', self._do_delete_network)

    def _r(self, path, method, handler):
        if not self._routes.get(path):
            self._routes[path] = {}
        self._routes[path][method] = handler

    @property
    def _peers(self):
        return [v.split(':')[0] for v in self._raft.peer_addresses]

    @property
    def _relay_ip(self):
        if self._raft.leader_address is not None:
            return self._raft.leader_address.split(':')[0]
        else:
            return None

    @staticmethod
    def install_plugin():
        logger.info('plugin.installing')
        if not os.path.isdir(SHOUTD_PLUGIN_DIR):
            try:
                os.remove(SHOUTD_PLUGIN_DIR)
            except FileNotFoundError:
                pass
            os.mkdir(SHOUTD_PLUGIN_DIR)

        spec_path = os.path.join(SHOUTD_PLUGIN_DIR, SHOUTD_SPEC_FILE)
        with open(spec_path, 'w+b') as f:
            f.write(('tcp://localhost:%d' % SHOUTD_PORT).encode('utf-8'))

    @staticmethod
    def uninstall_plugin():
        logger.info('plugin.uninstalling')
        spec_path = os.path.join(SHOUTD_PLUGIN_DIR, SHOUTD_SPEC_FILE)
        if os.path.exists(spec_path):
            try:
                os.remove(spec_path)
            except Exception as e:
                logger.error('unexpected', exception=e)

    async def _parse_request(self, reader):
        header_str = ''
        payload = b''
        while True:
            line = await reader.readline()
            if not line or line == b'\r\n':
                break
            header_str += line.decode('utf-8')

        match = length_regex.search(header_str)
        if match:
            length = int(match.group(1))
            while len(payload) < length:
                payload += await reader.read(2048)

        lines = header_str.split('\r\n')[:-1]
        method, url, _version = lines[0].split(' ')
        headers = lines[1:]
        return HttpRequest(method, url, headers, payload=payload)

    async def _handle_request(self, reader, writer):
        header_str = ''
        payload = b''
        request = await self._parse_request(reader)
        logger.debug('request.received', request=str(request))
        response = HttpResponse()
        if self._routes.get(request.path):
            handler = self._routes[request.path].get(request.method)
            if handler:
                response.payload = handler(request, response)
            else:
                response.status = 405
        else:
            response.status = 404
        logger.debug('response.sending', response=str(response))
        writer.write(response.data)
        await writer.drain()

    async def _handle_connection(self, reader, writer):
        try:
            await self._handle_request(reader, writer)
        except:
            response = HttpResponse(status=500)
            writer.write(response.data)
            await writer.drain()
            traceback.print_exc()
        finally:
            writer.close()

    def _handshake(self, request, response):
        logger.debug('handshake', implements=['NetworkDriver'])
        return {
            'Implements': ['NetworkDriver']
        }

    def _get_capabilities(self, request, response):
        logger.debug('capabilities.get', scope='global')
        return {
            'Scope': 'global'
        }

    def _connection_identifier(self, peer):
        if self._local_address < peer:
            s = self._local_address + peer
        else:
            s = peer + self._local_address

        return hashlib.md5(s.encode('utf-8')).hexdigest()

    def _create_tunnel(self, namespace, peer):
        ip = IPRoute()
        ns = NetNS(namespace)

        conn_id = self._connection_identifier(peer)
        conn_hash = hashlib.md5((conn_id + namespace).encode('utf-8')).hexdigest()
        vxlan_name = 'sdvx' + conn_hash[:6]
        vni = int(conn_hash[:6], 16)
        ip.link('add', ifname=vxlan_name, kind='vxlan', vxlan_id=vni,
                vxlan_local=self._local_address, vxlan_group=peer, vxlan_port=4789)
        vxlan = ip.link_lookup(ifname=vxlan_name)[0]
        ip.link('set', index=vxlan, net_ns_fd=namespace)
        vxlan = ns.link_lookup(ifname=vxlan_name)[0]
        ns.link('set', index=vxlan, mtu=1500)
        ns.link('set', index=vxlan, state='up')
        bridge = ns.link_lookup(ifname='shoutbr0')[0]
        ns.link('set', index=vxlan, master=bridge)

        ip.close()
        ns.close()

    def _create_namespace(self, namespace):
        ip = IPRoute()
        ns = NetNS(namespace)

        ns.link('add', ifname='shoutbr0', kind='bridge')
        bridge = ns.link_lookup(ifname='shoutbr0')[0]
        ns.link('set', index=bridge, mtu=1450)
        ns.link('set', index=bridge, state='up')

        if self._raft.is_leader:
            for peer in self._peers:
                self._create_tunnel(namespace, peer)
        else:
            while self._relay_ip is None:
                logger.warning('waiting')
                time.sleep(0.5)
            self._create_tunnel(namespace, self._relay_ip)

        ip.close()
        ns.close()

    def _delete_namespace(self, namespace):
        ns = NetNS(namespace)

        bridge = ns.link_lookup(ifname='shoutbr0')[0]
        ns.link('set', index=bridge, state='down')
        ns.link('del', index=bridge)

        ns.close()
        netns.remove(namespace)

    def _create_network(self, request, response):
        logger.debug('network.create')

        network_id = request['NetworkID']
        namespace = 'sdns' + network_id[:6]
        if namespace not in netns.listnetns():
            self._create_namespace(namespace)

        return {}

    def _do_delete_network(self, request, response):
        logger.debug('network.delete')

        network_id = request['NetworkID']
        namespace = 'sdns' + network_id[:6]
        if namespace in netns.listnetns():
            self._delete_namespace(namespace)

        return {}

    def _delete_network(self, request, response):
        for peer in self._peers:
            try:
                message = {'NetworkID': request['NetworkID']}
                endpoint = 'http://%s:%d/actions/delete-network' % (peer, SHOUTD_PORT)
                result = requests.post(endpoint, json=message, timeout=3)
                logger.debug('rpc', status=result.status_code, payload=result.text)
            except Exception as e:
                logger.error('unexpected', error=e)

        return self._do_delete_network(request, response)

    def _create_endpoint(self, request, response):
        logger.debug('endpoint.create')

        network_id = request['NetworkID']
        # if no CreateNetwork request received
        namespace = 'sdns' + network_id[:6]
        if namespace not in netns.listnetns():
            self._create_namespace(namespace)
        endpoint_id = request['EndpointID']
        address = request['Interface']['Address']
        ip = IPRoute()

        veth0_name = 'veth%s0' % endpoint_id[:6]
        veth1_name = 'veth%s1' % endpoint_id[:6]
        ip.link('add', ifname=veth0_name, kind='veth', peer=veth1_name)
        veth0 = ip.link_lookup(ifname=veth0_name)[0]
        veth1 = ip.link_lookup(ifname=veth1_name)[0]
        ip.link('set', index=veth0, mtu=1450)
        ip.link('set', index=veth1, mtu=1450)
        ip_addr, mask = address.split('/')
        ip.addr('add', index=veth1, address=ip_addr, mask=int(mask))

        ip.link('set', index=veth0, net_ns_fd=namespace)
        ns = NetNS(namespace)
        ns.link('set', index=veth0, state='up')
        bridge = ns.link_lookup(ifname='shoutbr0')[0]
        ns.link('set', index=veth0, master=bridge)

        ip.close()
        ns.close()
        return {
            'Interface': {}
        }

    def _get_endpoint_info(self, request, response):
        logger.debug('endpoint_info.get')
        return {
            'Value': {}
        }

    def _delete_endpoint(self, request, response):
        logger.debug('endpoint.delete')

        network_id = request['NetworkID']
        endpoint_id = request['EndpointID']
        veth0_name = 'veth%s0' % endpoint_id[:6]
        namespace = 'sdns' + network_id[:6]
        ns = NetNS(namespace)

        veth0 = ns.link_lookup(ifname=veth0_name)[0]
        ns.link('set', index=veth0, router=0)
        ns.link('set', index=veth0, state='down')
        ns.link('del', index=veth0)

        ns.close()
        return {}

    def _join(self, request, response):
        logger.debug('join')

        endpoint_id = request['EndpointID']
        veth1_name = 'veth%s1' % endpoint_id[:6]

        return {
            'InterfaceName': {
                'SrcName': veth1_name,
                'DstPrefix': 'shout'
            },
            'StaticRoutes': [{
                'Destination': '224.0.0.0/4',
                'RouteType': 1
            }]
        }

    def _leave(self, request, response):
        logger.debug('leave')
        return {}

    def _new_discovery(self, request, response):
        logger.debug('discovery.new')
        return {}

    def _delete_discovery(self, request, response):
        logger.debug('discovery.delete')
        return {}

    def run_forever(self):
        ShoutD.install_plugin()
        plugin_coro = asyncio.start_server(self._handle_connection, host='0.0.0.0', port=SHOUTD_PORT, loop=self._loop)
        tasks = asyncio.gather(
            asyncio.ensure_future(plugin_coro, loop=self._loop),
            *self._raft.tasks()
        )
        try:
            logger.info('listening.starting')
            self._loop.run_until_complete(tasks)
        except KeyboardInterrupt:
            self._raft.farewell()
            tasks.cancel()
            logger.info('listening.stopping')
            self._loop.run_forever()
            tasks.exception()
        finally:
            ShoutD.uninstall_plugin()
            self._loop.close()
