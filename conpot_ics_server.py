import struct
import logging
import json
import uuid
import random
import socket
import time
from datetime import datetime

import gevent
from gevent.server import StreamServer
from gevent.queue import Queue

from lxml import etree

import modbus_tk.modbus_tcp as modbus_tcp
import modbus_tk.defines as mdef
from modbus_tk import modbus
from modules import slave_db, feeder, sqlite_log, snmp_command_responder

import config

logger = logging.getLogger()


class ModbusServer(modbus.Server):
    def __init__(self, template, log_queue, databank=None):

        self.log_queue = log_queue

        """Constructor: initializes the server settings"""
        modbus.Server.__init__(self, databank if databank else modbus.Databank())

        dom = etree.parse(template)

        #parse slave configuration
        slaves = dom.xpath('//conpot_template/slaves/*')
        template_name = dom.xpath('//conpot_template/@name')[0]
        for s in slaves:
            id = int(s.attrib['id'])
            slave = self.add_slave(id)
            logger.debug('Added slave with id {0}.'.format(id))
            for b in s.xpath('./blocks/*'):
                name = b.attrib['name']
                type = eval('mdef.' + b.xpath('./type/text()')[0])
                start_addr = int(b.xpath('./starting_address/text()')[0])
                size = int(b.xpath('./size/text()')[0])
                slave.add_block(name, type, start_addr, size)
                logger.debug('Added block {0} to slave {1}. (type={2}, start={3}, size={4})'
                .format(name, id, type, start_addr, size))
                for v in b.xpath('./values/*'):
                    addr = int(v.xpath('./address/text()')[0])
                    value = eval(v.xpath('./content/text()')[0])
                    slave.set_values(name, addr, value)
                    logger.debug('Setting value at addr {0} to {1}.'.format(addr, v.xpath('./content/text()')[0]))

        logger.info('Conpot initialized using the {0} template.'.format(template_name))

    def handle(self, socket, address):
        session_id = str(uuid.uuid4())
        session_data = {'session_id': session_id, 'remote': address, 'timestamp': datetime.utcnow(),'data_type': 'modbus', 'data': {}}

        start_time = time.time()
        logger.info('New connection from {0}:{1}. ({2})'.format(address[0], address[1], session_id))

        socket.settimeout(5)
        fileobj = socket.makefile()

        try:
            while True:
                request = fileobj.read(7)
                if not request:
                    logger.info('Client disconnected. ({0})'.format(session_id))
                    break
                if request.strip().lower() == 'quit.':
                    logger.info('Client quit. ({0})'.format(session_id))
                    break
                tr_id, pr_id, length = struct.unpack(">HHH", request[:6])
                while len(request) < (length + 6):
                    new_byte = fileobj.read(1)
                    request += new_byte
                query = modbus_tcp.TcpQuery()

                #logdata is a dictionary containing request, slave_id, function_code and response
                response, logdata = self._databank.handle_request(query, request)
                elapse_ms = int(time.time() - start_time) * 1000
                session_data['data'][elapse_ms] = logdata

                logger.debug('Modbus traffic from {0}: {1} ({2})'.format(address[0], logdata, session_id))

                if response:
                    fileobj.write(response)
                    fileobj.flush()
        except socket.timeout:
            logger.debug('Socket timeout, remote: {0}. ({1})'.format(address[0], session_id))

        self.log_queue.put(session_data)

def log_worker(log_queue):
    if config.sqlite_enabled:
        sqlite_logger = sqlite_log.SQLiteLogger()
    if config.hpfriends_enabled:
        friends_feeder = feeder.HPFriendsLogger()

    while True:
        event = log_queue.get()
        assert 'data_type' in event
        assert 'timestamp' in event

        if config.hpfriends_enabled:
            friends_feeder.log(json.dumps(event))

        if config.sqlite_enabled:
            sqlite_logger.log(event)


def create_snmp_server(template, log_queue):
    dom = etree.parse(template)
    mibs = dom.xpath('//conpot_template/snmp/mibs/*')
    #only enable snmp server if we have configuration items
    if not mibs:
        snmp_server = None
    else:
        snmp_server = snmp_command_responder.CommandResponder(log_queue)

    for mib in mibs:
        mib_name = mib.attrib['name']
        for symbol in mib:
            symbol_name = symbol.attrib['name']
            value = symbol.xpath('./value/text()')[0]
            snmp_server.register(mib_name, symbol_name, value)
    return snmp_server


if __name__ == "__main__":

    root_logger = logging.getLogger()

    console_log = logging.StreamHandler()
    console_log.setLevel(logging.DEBUG)
    console_log.setFormatter(logging.Formatter('%(asctime)-15s %(message)s'))
    root_logger.addHandler(console_log)

    servers = []

    log_queue = Queue()
    gevent.spawn(log_worker, log_queue)

    logger.setLevel(logging.DEBUG)
    modbus_server = ModbusServer('templates/default.xml', log_queue, databank=slave_db.SlaveBase())
    connection = (config.host, config.port)
    server = StreamServer(connection, modbus_server.handle)
    logger.info('Modbus server started on: {0}'.format(connection))
    servers.append(gevent.spawn(server.serve_forever))

    snmp_server = create_snmp_server('templates/default.xml', log_queue)
    if snmp_server:
        logger.info('SNMP server started.')
        servers.append(gevent.spawn(snmp_server.serve_forever))

    gevent.joinall(servers)
