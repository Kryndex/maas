# Copyright 2005-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the maastftp Twisted plugin."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from functools import partial
import json
import os
import random
from socket import (
    AF_INET,
    AF_INET6,
    )
from urllib import urlencode
from urlparse import (
    parse_qsl,
    urlparse,
    )

from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
    )
from maastesting.testcase import (
    MAASTestCase,
    MAASTwistedRunTest,
    )
import mock
from mock import (
    sentinel,
    ANY,
    )
from netaddr import IPNetwork
from netaddr.ip import (
    IPV4_LINK_LOCAL,
    IPV6_LINK_LOCAL,
    )
from provisioningserver.boot import BytesReader
from provisioningserver.boot.pxe import PXEBootMethod
from provisioningserver.boot.tests.test_pxe import compose_config_path
from provisioningserver.events import EVENT_TYPES
from provisioningserver.pserv_services import tftp as tftp_module
from provisioningserver.pserv_services.tftp import (
    Port,
    TFTPBackend,
    TFTPService,
    UDPServer,
    )
from provisioningserver.tests.test_kernel_opts import make_kernel_parameters
from testtools.matchers import (
    AfterPreprocessing,
    AllMatch,
    Equals,
    IsInstance,
    MatchesAll,
    MatchesStructure,
    )
from tftp.backend import IReader
from tftp.protocol import TFTP
from twisted.application import internet
from twisted.application.service import MultiService
from twisted.internet import reactor
from twisted.internet.address import (
    IPv4Address,
    IPv6Address,
    )
from twisted.internet.defer import (
    inlineCallbacks,
    succeed,
    )
from twisted.internet.protocol import Protocol
from twisted.python import context
from zope.interface.verify import verifyObject


class TestBytesReader(MAASTestCase):
    """Tests for `provisioningserver.tftp.BytesReader`."""

    def test_interfaces(self):
        reader = BytesReader(b"")
        self.addCleanup(reader.finish)
        verifyObject(IReader, reader)

    def test_read(self):
        data = factory.make_string(size=10).encode("ascii")
        reader = BytesReader(data)
        self.addCleanup(reader.finish)
        self.assertEqual(data[:7], reader.read(7))
        self.assertEqual(data[7:], reader.read(7))
        self.assertEqual(b"", reader.read(7))

    def test_finish(self):
        reader = BytesReader(b"1234")
        reader.finish()
        self.assertRaises(ValueError, reader.read, 1)


class TestTFTPBackend(MAASTestCase):
    """Tests for `provisioningserver.tftp.TFTPBackend`."""

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_init(self):
        temp_dir = self.make_dir()
        generator_url = "http://%s.example.com/%s" % (
            factory.make_name("domain"), factory.make_name("path"))
        backend = TFTPBackend(temp_dir, generator_url)
        self.assertEqual((True, False), (backend.can_read, backend.can_write))
        self.assertEqual(temp_dir, backend.base.path)
        self.assertEqual(generator_url, backend.generator_url.geturl())

    def test_get_generator_url(self):
        # get_generator_url() merges the parameters obtained from the request
        # file path (arch, subarch, name) into the configured generator URL.
        mac = factory.make_mac_address("-")
        dummy = factory.make_name("dummy").encode("ascii")
        backend_url = b"http://example.com/?" + urlencode({b"dummy": dummy})
        backend = TFTPBackend(self.make_dir(), backend_url)
        # params is an example of the parameters obtained from a request.
        params = {"mac": mac}
        generator_url = urlparse(backend.get_generator_url(params))
        self.assertEqual("example.com", generator_url.hostname)
        query = parse_qsl(generator_url.query)
        query_expected = [
            ("dummy", dummy),
            ("mac", mac),
            ]
        self.assertItemsEqual(query_expected, query)

    def get_reader(self, data):
        temp_file = self.make_file(name="example", contents=data)
        temp_dir = os.path.dirname(temp_file)
        backend = TFTPBackend(temp_dir, "http://nowhere.example.com/")
        return backend.get_reader("example")

    @inlineCallbacks
    def test_get_reader_regular_file(self):
        # TFTPBackend.get_reader() returns a regular FilesystemReader for
        # paths not matching re_config_file.
        self.patch(tftp_module, 'send_event_node_mac_address')
        self.patch(tftp_module, 'get_remote_mac')
        data = factory.make_string().encode("ascii")
        reader = yield self.get_reader(data)
        self.addCleanup(reader.finish)
        self.assertEqual(len(data), reader.size)
        self.assertEqual(data, reader.read(len(data)))
        self.assertEqual(b"", reader.read(1))

    @inlineCallbacks
    def test_get_reader_handles_backslashes_in_path(self):
        self.patch(tftp_module, 'send_event_node_mac_address')
        self.patch(tftp_module, 'get_remote_mac')

        data = factory.make_string().encode("ascii")
        temp_dir = self.make_dir()
        subdir = factory.make_name('subdir')
        filename = factory.make_name('file')
        os.mkdir(os.path.join(temp_dir, subdir))
        factory.make_file(os.path.join(temp_dir, subdir), filename, data)

        path = '\\%s\\%s' % (subdir, filename)
        backend = TFTPBackend(temp_dir, "http://nowhere.example.com/")
        reader = yield backend.get_reader(path)

        self.addCleanup(reader.finish)
        self.assertEqual(len(data), reader.size)
        self.assertEqual(data, reader.read(len(data)))
        self.assertEqual(b"", reader.read(1))

    @inlineCallbacks
    def test_get_reader_logs_node_event_with_mac_address(self):
        mac_address = factory.make_mac_address()
        self.patch_autospec(tftp_module, 'send_event_node_mac_address')
        self.patch(tftp_module, 'get_remote_mac').return_value = mac_address
        data = factory.make_string().encode("ascii")
        reader = yield self.get_reader(data)
        self.addCleanup(reader.finish)
        self.assertThat(
            tftp_module.send_event_node_mac_address,
            MockCalledOnceWith(
                event_type=EVENT_TYPES.NODE_TFTP_REQUEST,
                mac_address=mac_address, description=ANY))

    @inlineCallbacks
    def test_get_reader_does_not_log_when_mac_cannot_be_found(self):
        self.patch_autospec(tftp_module, 'send_event_node_mac_address')
        self.patch(tftp_module, 'get_remote_mac').return_value = None
        data = factory.make_string().encode("ascii")
        reader = yield self.get_reader(data)
        self.addCleanup(reader.finish)
        self.assertThat(
            tftp_module.send_event_node_mac_address,
            MockNotCalled())

    @inlineCallbacks
    def _test_get_render_file(self, local, remote):
        # For paths matching PXEBootMethod.match_path, TFTPBackend.get_reader()
        # returns a Deferred that will yield a BytesReader.
        cluster_uuid = factory.make_UUID()
        self.patch(tftp_module, 'get_cluster_uuid').return_value = (
            cluster_uuid)
        self.patch(tftp_module, 'send_event_node_mac_address')
        mac = factory.make_mac_address("-")
        config_path = compose_config_path(mac)
        backend = TFTPBackend(self.make_dir(), b"http://example.com/")
        # python-tx-tftp sets up call context so that backends can discover
        # more about the environment in which they're running.
        call_context = {"local": local, "remote": remote}

        @partial(self.patch, backend, "get_boot_method_reader")
        def get_boot_method_reader(boot_method, params):
            params_json = json.dumps(params)
            params_json_reader = BytesReader(params_json)
            return succeed(params_json_reader)

        reader = yield context.call(
            call_context, backend.get_reader, config_path)
        output = reader.read(10000)
        # The addresses provided by python-tx-tftp in the call context are
        # passed over the wire as address:port strings.
        expected_params = {
            "mac": mac,
            "local": call_context["local"][0],  # address only.
            "remote": call_context["remote"][0],  # address only.
            "cluster_uuid": cluster_uuid,
            }
        observed_params = json.loads(output)
        self.assertEqual(expected_params, observed_params)

    def test_get_render_file_with_ipv4_hosts(self):
        return self._test_get_render_file(
            local=(
                factory.make_ipv4_address(),
                factory.pick_port()),
            remote=(
                factory.make_ipv4_address(),
                factory.pick_port()),
        )

    def test_get_render_file_with_ipv6_hosts(self):
        # Some versions of Twisted have the scope and flow info in the remote
        # address tuple. See https://twistedmatrix.com/trac/ticket/6826 (the
        # address is captured by tftp.protocol.TFTP.dataReceived).
        return self._test_get_render_file(
            local=(
                factory.make_ipv6_address(),
                factory.pick_port(),
                random.randint(1, 1000),
                random.randint(1, 1000)),
            remote=(
                factory.make_ipv6_address(),
                factory.pick_port(),
                random.randint(1, 1000),
                random.randint(1, 1000)),
        )

    @inlineCallbacks
    def test_get_boot_method_reader_returns_rendered_params(self):
        # get_boot_method_reader() takes a dict() of parameters and returns an
        # `IReader` of a PXE configuration, rendered by
        # `PXEBootMethod.get_reader`.
        backend = TFTPBackend(self.make_dir(), b"http://example.com/")
        # Fake configuration parameters, as discovered from the file path.
        fake_params = {"mac": factory.make_mac_address("-")}
        # Fake kernel configuration parameters, as returned from the API call.
        fake_kernel_params = make_kernel_parameters()

        # Stub get_page to return the fake API configuration parameters.
        fake_get_page_result = json.dumps(fake_kernel_params._asdict())
        get_page_patch = self.patch(backend, "get_page")
        get_page_patch.return_value = succeed(fake_get_page_result)

        # Stub get_reader to return the render parameters.
        method = PXEBootMethod()
        fake_render_result = factory.make_name("render").encode("utf-8")
        render_patch = self.patch(method, "get_reader")
        render_patch.return_value = BytesReader(fake_render_result)

        # Get the rendered configuration, which will actually be a JSON dump
        # of the render-time parameters.
        reader = yield backend.get_boot_method_reader(method, fake_params)
        self.addCleanup(reader.finish)
        self.assertIsInstance(reader, BytesReader)
        output = reader.read(10000)

        # The kernel parameters were fetched using `backend.get_page`.
        self.assertThat(backend.get_page, MockCalledOnceWith(mock.ANY))

        # The result has been rendered by `method.get_reader`.
        self.assertEqual(fake_render_result.encode("utf-8"), output)
        self.assertThat(method.get_reader, MockCalledOnceWith(
            backend, kernel_params=fake_kernel_params, **fake_params))

    @inlineCallbacks
    def test_get_boot_method_render_substitutes_armhf_in_params(self):
        # get_config_reader() should substitute "arm" for "armhf" in the
        # arch field of the parameters (mapping from pxe to maas
        # namespace).
        cluster_uuid = factory.make_UUID()
        self.patch(tftp_module, 'get_cluster_uuid').return_value = (
            cluster_uuid)
        self.patch(tftp_module, 'send_event_node_mac_address')
        config_path = "pxelinux.cfg/default-arm"
        backend = TFTPBackend(self.make_dir(), b"http://example.com/")
        # python-tx-tftp sets up call context so that backends can discover
        # more about the environment in which they're running.
        call_context = {
            "local": (
                factory.make_ipv4_address(),
                factory.pick_port()),
            "remote": (
                factory.make_ipv4_address(),
                factory.pick_port()),
            }

        @partial(self.patch, backend, "get_boot_method_reader")
        def get_boot_method_reader(boot_method, params):
            params_json = json.dumps(params)
            params_json_reader = BytesReader(params_json)
            return succeed(params_json_reader)

        reader = yield context.call(
            call_context, backend.get_reader, config_path)
        output = reader.read(10000)
        observed_params = json.loads(output)
        self.assertEqual("armhf", observed_params["arch"])


class TestTFTPService(MAASTestCase):

    def test_tftp_service(self):
        # A TFTP service is configured and added to the top-level service.
        interfaces = [
            factory.make_ipv4_address(),
            factory.make_ipv6_address(),
            ]
        self.patch(
            tftp_module, "get_all_interface_addresses",
            lambda: interfaces)
        example_root = self.make_dir()
        example_generator = "http://example.com/generator"
        example_port = factory.pick_port()
        tftp_service = TFTPService(
            resource_root=example_root, generator=example_generator,
            port=example_port)
        tftp_service.updateServers()
        # The "tftp" service is a multi-service containing UDP servers for
        # each interface defined by get_all_interface_addresses().
        self.assertIsInstance(tftp_service, MultiService)
        # There's also a TimerService that updates the servers every 45s.
        self.assertThat(
            tftp_service.refresher, MatchesStructure.byEquality(
                step=45, parent=tftp_service, name="refresher",
                call=(tftp_service.updateServers, (), {}),
            ))
        expected_backend = MatchesAll(
            IsInstance(TFTPBackend),
            AfterPreprocessing(
                lambda backend: backend.base.path,
                Equals(example_root)),
            AfterPreprocessing(
                lambda backend: backend.generator_url.geturl(),
                Equals(example_generator)))
        expected_protocol = MatchesAll(
            IsInstance(TFTP),
            AfterPreprocessing(
                lambda protocol: protocol.backend,
                expected_backend))
        expected_server = MatchesAll(
            IsInstance(internet.UDPServer),
            AfterPreprocessing(
                lambda service: len(service.args),
                Equals(2)),
            AfterPreprocessing(
                lambda service: service.args[0],  # port
                Equals(example_port)),
            AfterPreprocessing(
                lambda service: service.args[1],  # protocol
                expected_protocol))
        self.assertThat(
            tftp_service.getServers(),
            AllMatch(expected_server))
        # Only the interface used for each service differs.
        self.assertItemsEqual(
            [svc.kwargs for svc in tftp_service.getServers()],
            [{"interface": interface} for interface in interfaces])

    def test_tftp_service_rebinds_on_HUP(self):
        # Initial set of interfaces to bind to.
        interfaces = {"1.1.1.1", "2.2.2.2"}
        self.patch(
            tftp_module, "get_all_interface_addresses",
            lambda: interfaces)

        tftp_service = TFTPService(
            resource_root=self.make_dir(), generator="http://mighty/wind",
            port=factory.pick_port())
        tftp_service.updateServers()

        # The child services of tftp_services are named after the
        # interface they bind to.
        self.assertEqual(interfaces, {
            server.name for server in tftp_service.getServers()
        })

        # Update the set of interfaces to bind to.
        interfaces.add("3.3.3.3")
        interfaces.remove("1.1.1.1")

        # Ask the TFTP service to update its set of servers.
        tftp_service.updateServers()

        # We're in the reactor thread but we want to move the reactor
        # forwards, hence we need to get all explicit about it.
        reactor.runUntilCurrent()

        # The interfaces now bound match the updated interfaces set.
        self.assertEqual(interfaces, {
            server.name for server in tftp_service.getServers()
        })

    def test_tftp_service_does_not_bind_to_link_local_addresses(self):
        # Initial set of interfaces to bind to.
        ipv4_test_net_3 = IPNetwork("203.0.113.0/24")  # RFC 5737
        normal_addresses = {
            factory.pick_ip_in_network(ipv4_test_net_3),
            factory.make_ipv6_address(),
        }
        link_local_addresses = {
            factory.pick_ip_in_network(IPV4_LINK_LOCAL),
            factory.pick_ip_in_network(IPV6_LINK_LOCAL),
        }
        self.patch(
            tftp_module, "get_all_interface_addresses",
            lambda: normal_addresses | link_local_addresses)

        tftp_service = TFTPService(
            resource_root=self.make_dir(), generator="http://mighty/wind",
            port=factory.pick_port())
        tftp_service.updateServers()

        # Only the "normal" addresses have been used.
        self.assertEqual(normal_addresses, {
            server.name for server in tftp_service.getServers()
        })


class DummyProtocol(Protocol):
    def doStop(self):
        pass


class TestPort(MAASTestCase):
    """Tests for :py:class:`Port`."""

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_getHost_works_with_IPv4_address(self):
        port = Port(0, DummyProtocol(), "127.0.0.1")
        port.addressFamily = AF_INET
        port.startListening()
        self.addCleanup(port.stopListening)
        self.assertEqual(
            IPv4Address('UDP', '127.0.0.1', port._realPortNumber),
            port.getHost())

    def test_getHost_works_with_IPv6_address(self):
        port = Port(0, DummyProtocol(), "::1")
        port.addressFamily = AF_INET6
        port.startListening()
        self.addCleanup(port.stopListening)
        self.assertEqual(
            IPv6Address('UDP', '::1', port._realPortNumber),
            port.getHost())


class TestUDPServer(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__getPort_calls__listenUDP_with_args_from_constructor(self):
        server = UDPServer(sentinel.foo, bar=sentinel.bar)
        _listenUDP = self.patch(server, "_listenUDP")
        _listenUDP.return_value = sentinel.port
        self.assertEqual(sentinel.port, server._getPort())
        self.assertThat(_listenUDP, MockCalledOnceWith(
            sentinel.foo, bar=sentinel.bar))

    def test__listenUDP_with_IPv4_address(self):
        server = UDPServer(0, DummyProtocol(), "127.0.0.1")
        port = server._getPort()
        self.addCleanup(port.stopListening)
        self.assertEqual(AF_INET, port.addressFamily)

    def test__listenUDP_with_IPv6_address(self):
        server = UDPServer(0, DummyProtocol(), "::1")
        port = server._getPort()
        self.addCleanup(port.stopListening)
        self.assertEqual(AF_INET6, port.addressFamily)