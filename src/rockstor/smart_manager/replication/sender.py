"""
Copyright (c) 2012-2013 RockStor, Inc. <http://rockstor.com>
This file is part of RockStor.

RockStor is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published
by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

RockStor is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

from multiprocessing import Process
import os
import sys
import logging
import zmq
import subprocess
import fcntl
import json
from django.conf import settings
import time
from datetime import datetime
from contextlib import contextmanager
from django.utils.timezone import utc
from util import (create_replica_trail, update_replica_status, is_snapshot,
                  create_snapshot)

BTRFS = '/sbin/btrfs'
logger = logging.getLogger(__name__)


class Sender(Process):

    def __init__(self, replica, sender_ip, pub, q, snap_name, data_port,
                 meta_port, rt=None):
        self.replica = replica
        self.receiver_ip = self.replica.appliance
        self.meta_port = meta_port
        self.data_port = data_port
        self.sender_ip = sender_ip
        self.pub = pub
        self.q = q
        self.snap_name = snap_name
        self.rt = rt
        self.rt2 = None
        self.rt2_id = None
        self.ppid = os.getpid()
        self.snap_id = ('%s_%s_%s_%s' %
                        (self.sender_ip, self.replica.pool, self.replica.share,
                         self.snap_name))
        self.snap_id = str(self.snap_id)
        self.meta_begin = {'id': self.snap_id,
                           'msg': 'begin',
                           'pool': self.replica.dpool,
                           'share': self.replica.share,
                           'snap': self.snap_name,
                           'ip': self.sender_ip,
                           'data_port': self.data_port,
                           'meta_port': self.meta_port,
                           'incremental': self.rt is not None,}
        self.meta_end = {'id': self.snap_id,
                         'msg': 'end',}
        self.kb_sent = 0
        super(Sender, self).__init__()

    @contextmanager
    def _clean_exit_handler(self, msg):
        try:
            yield
        except Exception, e:
            logger.error(msg)
            logger.exception(e)
            sys.exit(3)

    @contextmanager
    def _update_trail_and_quit(self, msg):
        try:
            yield
        except Exception, e:
            logger.error(msg)
            logger.exception(e)
            try:
                data = {'status': 'failed',
                        'error': msg,
                        'end_ts': datetime.utcnow().replace(tzinfo=utc),}
                update_replica_status(self.rt2_id, data, logger)
            except Exception, e:
                logger.error('Exception occured in cleanup handler')
                logger.exception(e)
            finally:
                sys.exit(3)

    def _process_q(self):
        ack = self.q.get(block=True, timeout=60)
        if (ack['msg'] == 'error'):
            error = 'Error on Receiver: %s' % ack['error']
            with self._update_trail_and_quit(error):
                raise Exception('got error from receiver')
        return ack

    def run(self):
        msg = ('Failed to connect to receiver(%s) on meta port'
               '(%d) for snap_name: %s. Aborting.' %
               (self.receiver_ip, self.meta_port, self.snap_name))
        with self._clean_exit_handler(msg):
            ctx = zmq.Context()
            meta_push = ctx.socket(zmq.PUSH)
            meta_push.connect('tcp://%s:%d' % (self.receiver_ip,
                                               self.meta_port))

        #1. create a new replica trail if it's the very first time
        # of if the last one succeeded
        msg = ('Failed to create local replica trail for snap_name:'
               ' %s. Aborting.' % self.snap_name)
        with self._clean_exit_handler(msg):
            self.rt2 = create_replica_trail(self.replica.id,
                                            self.snap_name, logger)
            self.rt2_id = self.rt2['id']

        #2. create a snapshot only if it's not already from a previous
        #failed attempt.
        if (not is_snapshot(self.replica.share, self.snap_name, logger)):
            msg = ('Failed to create snapshot: %s. Aborting.' % self.snap_name)
            with self._clean_exit_handler(msg):
                create_snapshot(self.replica.share, self.snap_name, logger)

        #let the receiver know that following diff is coming
        msg = ('Failed to send initial metadata communication to the '
               'receiver(%s), most likely due to a network error. Aborting.'
               % self.receiver_ip)
        with self._update_trail_and_quit(msg):
            logger.debug('sending meta_begin')
            meta_push.send_json(self.meta_begin)
            logger.debug('meta_begin sent. waiting on get')

        msg = ('Timeout occured(60 seconds) while waiting for OK '
               'from the receiver(%s) to start sending data. Aborting.'
               % self.receiver_ip)
        with self._update_trail_and_quit(msg):
            self._process_q()

        snap_path = ('%s%s/%s_%s' % (settings.MNT_PT, self.replica.pool,
                                     self.replica.share, self.snap_name))
        logger.debug('current snap: %s' % snap_path)
        cmd = [BTRFS, 'send', snap_path]
        if (self.rt is not None):
            prev_snap = ('%s%s/%s_%s' % (settings.MNT_PT, self.replica.pool,
                                         self.replica.share,
                                         self.rt.snap_name))
            logger.info('Sending incremental replica between %s -- %s' %
                        (prev_snap, snap_path))
            cmd = [BTRFS, 'send', '-p', prev_snap, snap_path]
        else:
            logger.info('Sending full replica: %s' % snap_path)

        try:
            sp = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            fcntl.fcntl(sp.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
            logger.debug('send started. snap: %s' % snap_path)
        except Exception, e:
            msg = ('Failed to start the low level btrfs send '
                   'command(%s). Aborting' % cmd)
            logger.error(msg)
            logger.exception(e)
            with self._update_trail_and_quit(msg):
                self.pub.put('%sEND_FAIL' % self.snap_id)
            sys.exit(3)

        alive = True
        while alive:
            try:
                if (sp.poll() is not None):
                    logger.debug('send process finished. rc: %d. stderr: %s'
                                 % (sp.returncode, sp.stderr.read()))
                    alive = False
                fs_data = sp.stdout.read()
            except IOError:
                continue
            except Exception, e:
                msg = ('Exception occured while reading low level btrfs '
                       'send data for snap_name: %s. Aborting.' %
                       self.snap_name)
                if (alive):
                    sp.terminate()
                with self._update_trail_and_quit(msg):
                    self.pub.put('%sEND_FAIL' % self.snap_id)
                    raise e

            msg = ('Failed to send fsdata to the receiver for snap_name: '
                   '%s. Aborting.' % (self.snap_name))
            with self._update_trail_and_quit(msg):
                self.pub.put('%s%s' % (self.snap_id, fs_data))
                self.kb_sent = self.kb_sent + len(fs_data)
                logger.debug('send process still alive. kb_sent: %s' %
                             self.kb_sent)

                if (not alive):
                    if (sp.returncode != 0):
                        self.pub.put('%sEND_FAIL' % self.snap_id)
                    else:
                        self.pub.put('%sEND_SUCCESS' % self.snap_id)
                        logger.debug('sent END for snap_name: %s' %
                                     self.snap_name)

            if (os.getppid() != self.ppid):
                logger.error('Scheduler exited. Sender for snap_name: '
                             '%s cannot go on. Aborting.'
                             % self.snap_name)
                sys.exit(3)

        logger.debug('send process finished. blocking')
        msg = ('Timeout occured(60 seconds) while waiting for final '
               'send confirmation from the receiver(%s) for snap_name:'
               ' %s. Aborting.' % (self.receiver_ip, self.snap_name))
        with self._update_trail_and_quit(msg):
            ack = self._process_q()

        logger.debug('fsdata sent, confirmation: %s received' % ack)
        end_ts = datetime.utcnow().replace(tzinfo=utc)
        data = {'status': 'succeeded',
                'kb_sent': self.kb_sent / 1024,
                'end_ts' : end_ts,}
        if (ack['msg'] == 'receive_error'):
            msg = ('Receiver(%s) returned a processing error for snap_name:'
                   ' %s. Check it for more information.'
                   % (self.receiver_ip, self.snap_name))
            data['status'] = 'failed'
            data['error'] = msg
            data['send_failed'] = end_ts

        msg = ('Failed to update final replica status for snap_name: %s'
               '. Aborting.' % self.snap_name)
        with self._clean_exit_handler(msg):
            update_replica_status(self.rt2_id, data, logger)

