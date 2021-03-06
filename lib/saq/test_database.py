# vim: sw=4:ts=4:et

import logging
import uuid
import unittest
import threading
import multiprocessing

from multiprocessing import Process, Event, Pipe

import saq
import saq.database
import saq.test

from saq.constants import *
from saq.database import get_db_connection, Alert, \
                         enable_cached_db_connections, disable_cached_db_connections
from saq.test import *

def reset_database(target_function):
    def wrapper(*args, **kwargs):
        with get_db_connection() as db:
            c = db.cursor()
            c.execute("DELETE FROM alerts")
            db.commit()
            return target_function(*args, **kwargs)

    return wrapper

class DatabaseTestCase(ACEBasicTestCase):

    def insert_alert(self):
        root = create_root_analysis(uuid=str(uuid.uuid4()))
        root.initialize_storage()
        root.save()

        alert = Alert(storage_dir=root.storage_dir)
        alert.load()
        alert.sync()
        self.assertIsNotNone(alert.id)
        return alert

    def test_database_000_connection(self):
        with get_db_connection() as db:
            pass

        session = saq.database.DatabaseSession()
        self.assertIsNotNone(session)

    @reset_database
    def test_database_001_insert_alert(self):
        alert = self.insert_alert()

    @reset_database
    def test_database_002_lock(self):
        alert = self.insert_alert()

        self.assertTrue(alert.lock())
        # something that was locked is locked
        self.assertTrue(alert.is_locked())
        # and cannot be locked again
        self.assertFalse(alert.lock())
        # can be unlocked
        self.assertTrue(alert.unlock())
        # truely is unlocked
        self.assertFalse(alert.is_locked())
        # cannot be unlocked again  
        self.assertFalse(alert.unlock())
        # and can be locked again
        self.assertTrue(alert.lock())
        self.assertTrue(alert.is_locked())

    @reset_database
    #@unittest.skip("...")
    def test_database_003_multiprocess_lock(self):
        alert = self.insert_alert()
        sync0 = Event()
        sync1 = Event()
        sync2 = Event()

        def p1(alert_id):
            session = saq.database.DatabaseSession()
            alert = session.query(Alert).filter(Alert.id == alert_id).one()
            alert.lock()
            # tell parent to get the lock
            sync0.set()
            # wait for parent to signal
            sync1.wait()
            alert.unlock()
            sync2.set()

        p = Process(target=p1, args=(alert.id,))
        p.start()

        try:
            sync0.wait()
            
            # lock should already be locked
            self.assertTrue(alert.is_locked())
            # should not be able to lock the lock
            self.assertFalse(alert.lock())
            # and should not be able to unlock the lock
            self.assertFalse(alert.unlock())
            self.assertTrue(alert.is_locked())

            sync1.set()
            sync2.wait()
            # lock should be unlocked
            self.assertFalse(alert.is_locked())
            # and we should be able to lock it
            self.assertTrue(alert.lock())
            self.assertTrue(alert.is_locked())
            self.assertTrue(alert.unlock())
            self.assertFalse(alert.is_locked())
            
            p.join()
            p = None
        finally:
            if p:
                p.terminate()
                p.join()

    @reset_database
    @reset_config
    def test_database_003_expired(self):
        # set locks to expire immediately
        saq.CONFIG['global']['lock_timeout'] = '00:00'
        alert = self.insert_alert()
        self.assertTrue(alert.lock())
        # should expire right away
        self.assertFalse(alert.is_locked())
        self.assertTrue(alert.has_current_lock())
        # and we are able to lock it again
        self.assertTrue(alert.lock())

    @reset_database
    def test_database_004_lock_proxy(self):
        alert = self.insert_alert()
        pipe_p, pipe_c = Pipe()

        def p1(pipe_c):
            # get the lock proxy from the queue
            proxy = pipe_c.recv()
            # unlock the lock and send the result through the pipe
            pipe_c.send(proxy.unlock())
            pipe_c.close()
        
        p = Process(target=p1, args=(pipe_c,))
        p.start()

        try:
            self.assertTrue(alert.lock())
            proxy = alert.create_lock_proxy()
            alert.transfer_locks_to(proxy)
            pipe_p.send(proxy)
            self.assertTrue(pipe_p.recv())
            pipe_p.close()
            p.join()
            p = None

            self.assertFalse(alert.is_locked())

        finally:
            if p:
                p.terminate()
                p.join()

        # do it again but this time don't transfer the locks
        # it should fail to unlock

        alert = self.insert_alert()
        pipe_p, pipe_c = Pipe()

        p = Process(target=p1, args=(pipe_c,))
        p.start()

        try:
            self.assertTrue(alert.lock())
            proxy = alert.create_lock_proxy()
            #alert.transfer_locks_to(proxy)
            pipe_p.send(proxy)
            self.assertFalse(pipe_p.recv())
            pipe_p.close()
            p.join()
            p = None

            self.assertTrue(alert.is_locked())

        finally:
            if p:
                p.terminate()
                p.join()

    def test_database_005_caching(self):
        from saq.database import _cached_db_connections_enabled

        self.assertFalse(_cached_db_connections_enabled())
        enable_cached_db_connections()
        self.assertTrue(_cached_db_connections_enabled())
        with get_db_connection() as db:
            pass

        # we should have one database connection ready
        self.assertEquals(len(saq.database._global_db_cache), 1)

        disable_cached_db_connections()
        self.assertFalse(_cached_db_connections_enabled())

        # we should have zero database connection ready
        self.assertEquals(len(saq.database._global_db_cache), 0)
        self.assertEquals(len(saq.database._use_cache_flags), 0)

    def test_database_006_caching_threaded(self):
        """Cached database connections for threads."""
        enable_cached_db_connections()
        e = threading.Event()
        with get_db_connection() as conn_1:
            self.assertEquals(len(saq.database._global_db_cache), 1)
            conn_1_id = id(conn_1)

            def f():
                enable_cached_db_connections()
                # this connection should be different than conn_1
                with get_db_connection() as conn_2:
                    self.assertEquals(len(saq.database._global_db_cache), 2)
                    self.assertNotEquals(conn_1, conn_2)
                    conn_2_id = id(conn_2)

                # but asked a second time this should be the same as before
                with get_db_connection() as conn_3:
                    self.assertEquals(len(saq.database._global_db_cache), 2)
                    self.assertEquals(conn_2_id, id(conn_3))

                e.set()
                disable_cached_db_connections()
                self.assertEquals(len(saq.database._global_db_cache), 1)
                
            t = threading.Thread(target=f)
            t.start()
            e.wait()

        with get_db_connection() as conn_4:
            self.assertEquals(len(saq.database._global_db_cache), 1)
            self.assertEquals(conn_1_id, id(conn_4))

        disable_cached_db_connections()
        self.assertEquals(len(saq.database._global_db_cache), 0)

    def test_database_007_caching_processes(self):
        """Cached database connections for processes."""
        enable_cached_db_connections()
        with get_db_connection() as conn_1:
            self.assertEquals(len(saq.database._global_db_cache), 1)
            conn_1_id = id(conn_1)

            def f():
                enable_cached_db_connections()
                # this connection should be different than conn_1
                with get_db_connection() as conn_2:
                    send_test_message(len(saq.database._global_db_cache) == 2)
                    send_test_message(conn_1 != conn_2)
                    conn_2_id = id(conn_2)

                # but asked a second time this should be the same as before
                with get_db_connection() as conn_3:
                    send_test_message(len(saq.database._global_db_cache) == 2)
                    send_test_message(conn_2_id == id(conn_3))

                disable_cached_db_connections()
                send_test_message(len(saq.database._global_db_cache) == 1)
                
            p = multiprocessing.Process(target=f)
            p.start()

            self.assertTrue(recv_test_message()) # len(saq.database._global_db_cache) == 2
            self.assertTrue(recv_test_message()) # conn_1 != conn_2
            self.assertTrue(recv_test_message()) # len(saq.database._global_db_cache) == 2
            self.assertTrue(recv_test_message()) # conn_2_id == id(conn_3)
            self.assertTrue(recv_test_message()) # len(saq.database._global_db_cache) == 1

            p.join()

        with get_db_connection() as conn_4:
            self.assertEquals(len(saq.database._global_db_cache), 1)
            self.assertEquals(conn_1_id, id(conn_4))

        disable_cached_db_connections()
        self.assertEquals(len(saq.database._global_db_cache), 0)
