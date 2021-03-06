# vim: sw=4:ts=4:et

__all__ = [
    'EV_TEST_DATE',
    'EV_ROOT_ANALYSIS_TOOL',
    'EV_ROOT_ANALYSIS_TOOL_INSTANCE',
    'EV_ROOT_ANALYSIS_ALERT_TYPE',
    'EV_ROOT_ANALYSIS_DESCRIPTION',
    'EV_ROOT_ANALYSIS_EVENT_TIME',
    'EV_ROOT_ANALYSIS_NAME',
    'EV_ROOT_ANALYSIS_UUID',
    'create_root_analysis',
    'ACEBasicTestCase',
    'ACEEngineTestCase',
    'ACEModuleTestCase',
    'modify_logging_level',
    'reset_config',
    'cleanup_delayed_analysis',
    'log_count',
    'wait_for_log_count',
    'clear_log',
    'WaitTimedOutError',
    'wait_for_log_entry',
    'track_io',
    'send_test_message',
    'recv_test_message',
    'splunk_query',
    'wait_for',
    'enable_module',
    'force_alerts',
    'protect_production',
    'GUIServer',
    'search_log',
]

import atexit
import datetime
import logging
import os, os.path
import shutil
import sys
import threading
import time

from multiprocessing import Manager, RLock, Pipe
from unittest import TestCase
from subprocess import Popen, PIPE

import saq
from saq.analysis import RootAnalysis, _enable_io_tracker, _disable_io_tracker
from saq.database import initialize_database, get_db_connection
from saq.error import report_exception

from splunklib import SplunkQueryObject

initialized = False
test_dir = None

# decorators
#
def reset_config(target_function):
    def wrapper(*args, **kwargs):
        try:
            return target_function(*args, **kwargs)
        finally:
            saq.load_configuration()
    return wrapper

def modify_logging_level(level):
    def modify_logging_level_decorator(target_function):
        def wrapper(*args, **kwargs):
            current_level = logging.getLogger().getEffectiveLevel()
            logging.getLogger().setLevel(level)
            try:
                return target_function(*args, **kwargs)
            finally:
                logging.getLogger().setLevel(current_level)
        return wrapper
    return modify_logging_level_decorator

def track_io(target_function):
    def wrapper(*args, **kwargs):
        try:
            _enable_io_tracker()
            return target_function(*args, **kwargs)
        finally:
            _disable_io_tracker()
    return wrapper

def clear_log(target_function):
    def wrapper(*args, **kwargs):
        memory_log_handler.clear()
        return target_function(*args, **kwargs)
    return wrapper

def cleanup_delayed_analysis(target_function):
    def wrapper(*args, **kwargs):
        try:
            return target_function(*args, **kwargs)
        finally:
            delayed_analysis_dir = os.path.join(saq.SAQ_HOME, 'var', 'unittest', 'delayed_analysis')
            if os.path.exists(delayed_analysis_dir):
                shutil.rmtree(delayed_analysis_dir)
    return wrapper

def force_alerts(target_function):
    """Alerts will be forced ON for the duration of this function."""
    def wrapper(*args, **kwargs):
        try:
            saq.FORCED_ALERTS = True
            return target_function(*args, **kwargs)
        finally:
            saq.FORCED_ALERTS = False
    return wrapper

def protect_production(target_function):
    """Do not allow this to run on a production system."""
    def wrapper(*args, **kwargs):
        if saq.CONFIG['global']['instance_type'] not in [ 'PRODUCTION', 'QA', 'DEV' ]:
            sys.stderr.write('\n\n *** CRITICAL ERROR *** \n\ninvalid instance_type setting in configuration\n')
            sys.exit(1)

        if saq.CONFIG['global']['instance_type'] == 'PRODUCTION':
            sys.stderr.write('\n\n *** PROTECT PRODUCTION *** \ndo not execute this in production, idiot\n')
            sys.exit(1)

        return target_function(*args, **kwargs)

    return wrapper

# 
# utility functions

def enable_module(engine_name, module_name):
    """Adds a module to be enabled."""
    saq.CONFIG[module_name]['enabled'] = 'yes'
    saq.CONFIG[engine_name][module_name] = 'yes'

def wait_for(condition, interval=1, timeout=8):
    """Wait for condition to return True, checking every interval seconds until timeout seconds have elapsed.
       Return True if condition returned True before timeout was exceeded, False otherwise."""
    timeout = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
    while datetime.datetime.now() < timeout:
        if condition():
            return True

        time.sleep(interval)

    return False

# test comms pipe is used to communicate between test process and child processes
test_comms_p = None
test_comms_pid = None
test_comms_c = None

def open_test_comms():
    global test_comms_p
    global test_comms_pid
    global test_comms_c

    test_comms_p, test_comms_c = Pipe()
    test_comms_pid = os.getpid()
    
def close_test_comms():
    test_comms_p.close()
    test_comms_c.close()

def get_test_comm_pipe():
    # if we are the original process then we use the "parent" pipe
    # otherwise we use the "child" pipe
    if os.getpid() == test_comms_pid:
        return test_comms_p

    return test_comms_c

def send_test_message(message):
    get_test_comm_pipe().send(message)

def recv_test_message():
    return get_test_comm_pipe().recv()

test_log_manager = None
test_log_sync = None
test_log_messages = None
memory_log_handler = None

class WaitTimedOutError(Exception):
    pass

#
# custom logging

class MemoryLogHandler(logging.Handler):
    def acquire(self):
        test_log_sync.acquire()

    def release(self):
        test_log_sync.release()

    def createLock(self):
        pass

    def emit(self, record):
        try:
            test_log_messages.append(record)
        except:
            sys.stderr.write(str(record) + "\n")

    def clear(self):
        with test_log_sync:
            del test_log_messages[:]

    def search(self, condition):
        """Searches and returns all log records for which condition(record) was True. Returns the list of LogRecord that matched."""

        result = []
        with test_log_sync:
            for message in test_log_messages:
                if condition(message):
                    result.append(message)

        return result

    def wait_for_log_entry(self, callback, timeout=5, count=1):
        """Waits for callback to return True count times before timeout seconds expire.
           callback takes a single LogRecord object as the parameter and returns a boolean."""
        time_limit = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        current_index = 0
        current_count = 0

        while True:
            with test_log_sync:
                while current_index < len(test_log_messages):
                    if callback(test_log_messages[current_index]):
                        current_count += 1

                        if current_count == count:
                            return True

                    current_index += 1

            if datetime.datetime.now() >= time_limit:
                raise WaitTimedOutError()

            time.sleep(0.1)

def _atexit_callback():
    global test_log_manager

    if test_log_manager:
        try:
            test_log_manager.shutdown()
        except Exception as e:
            print("ERROR: unable to shutdown test log manager: {}".format(e))
            
def initialize_unittest_logging():
    # ACE is multi-process multi-threaded
    # so we use this special logging mechanism to keep a central repository of the log events generated
    # that the original process can access

    global test_log_manager
    global test_log_sync
    global test_log_messages
    global memory_log_handler

    test_log_manager = Manager()
    atexit.register(_atexit_callback)
    test_log_sync = RLock()
    test_log_messages = test_log_manager.list()

    log_format = logging.Formatter(datefmt='%(asctime)s')

    memory_log_handler = MemoryLogHandler()
    memory_log_handler.setLevel(logging.DEBUG)
    memory_log_handler.setFormatter(log_format)
    logging.getLogger().addHandler(memory_log_handler)

def wait_for_log_entry(*args, **kwargs):
    return memory_log_handler.wait_for_log_entry(*args, **kwargs)

def log_count(text):
    """Returns the number of times the given text is seen in the logs."""
    with test_log_sync:
        return len([x for x in test_log_messages if text in x.getMessage()])

def wait_for_log_count(text, count, timeout=5):
    """Waits for text to occur count times in the logs before timeout seconds elapse."""
    def condition(e):
        return text in e.getMessage()

    return memory_log_handler.wait_for_log_entry(condition, timeout, count)

def search_log(text):
    return memory_log_handler.search(lambda log_record: text in log_record.getMessage())

def splunk_query(search_string, *args, **kwargs):
    config = saq.CONFIG['splunk']
    q = SplunkQueryObject(
        uri=config['uri'],
        username=config['username'],
        password=config['password'],
        *args, **kwargs)

    result = q.query(search_string)
    return q, result

def initialize_test_environment():
    global initialized
    global test_dir

    if initialized:
        return

    # there is no reason to run anything as root
    if os.geteuid() == 0:
        print("do not run ace as root please")
        sys.exit(1)

    # where is ACE?
    saq_home = '/opt/saq'
    if 'SAQ_HOME' in os.environ:
        saq_home = os.environ['SAQ_HOME']

    # adjust search path
    sys.path.append(os.path.join(saq_home, 'lib'))

    # initialize saq
    import saq
    saq.initialize(saq_home=saq_home, config_paths=[], 
                   logging_config_path=os.path.join(saq_home, 'etc', 'unittest_logging.ini'), 
                   args=None, relative_dir=None)

    # additional logging required for testing
    initialize_unittest_logging()

    # create a temporary storage directory
    test_dir = os.path.join(saq.SAQ_HOME, 'var', 'test')
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except Exception as e:
            logging.error("unable to delete {}: {}".format(test_dir, e))
            sys.exit(1)

    try:
        os.makedirs(test_dir)
    except Exception as e:
        logging.error("unable to create temp dir {}: {}".format(test_dir, e))

    # in all our testing we use the password "password" for encryption/decryption
    from saq.crypto import get_aes_key
    saq.ENCRYPTION_PASSWORD = get_aes_key('password')

    initialize_database()
    initialized = True

# expected values
EV_TEST_DATE = datetime.datetime(2017, 11, 11, hour=7, minute=36, second=1, microsecond=1)

EV_ROOT_ANALYSIS_TOOL = 'test_tool'
EV_ROOT_ANALYSIS_TOOL_INSTANCE = 'test_tool_instance'
EV_ROOT_ANALYSIS_ALERT_TYPE = 'test_alert'
EV_ROOT_ANALYSIS_DESCRIPTION = 'This is only a test.'
EV_ROOT_ANALYSIS_EVENT_TIME = EV_TEST_DATE
EV_ROOT_ANALYSIS_NAME = 'test'
EV_ROOT_ANALYSIS_UUID = '14ca0ff2-ff7e-4fa1-a375-160dc072ab02'

def create_root_analysis(tool=None, tool_instance=None, alert_type=None, desc=None, event_time=None,
                         action_counts=None, details=None, name=None, remediation=None, state=None,
                         uuid=None, location=None, storage_dir=None, company_name=None, company_id=None):
    """Returns a default RootAnalysis object with expected values for testing."""
    return RootAnalysis(tool=tool if tool else EV_ROOT_ANALYSIS_TOOL,
                        tool_instance=tool_instance if tool_instance else EV_ROOT_ANALYSIS_TOOL_INSTANCE,
                        alert_type=alert_type if alert_type else EV_ROOT_ANALYSIS_ALERT_TYPE,
                        desc=desc if desc else EV_ROOT_ANALYSIS_DESCRIPTION,
                        event_time=event_time if event_time else EV_TEST_DATE,
                        action_counters=action_counts if action_counts else None,
                        details=details if details else None, 
                        name=name if name else EV_ROOT_ANALYSIS_NAME,
                        remediation=remediation if remediation else None,
                        state=state if state else None,
                        uuid=uuid if uuid else EV_ROOT_ANALYSIS_UUID,
                        location=location if location else None,
                        storage_dir=storage_dir if storage_dir else os.path.join(saq.SAQ_HOME, 'var', 'test', uuid if uuid else EV_ROOT_ANALYSIS_UUID),
                        company_name=company_name if company_name else None,
                        company_id=company_id if company_id else None)

class ServerProcess(object):
    def __init__(self, args):
        self.args = args
        self.process = None
        self.stdout_reader = None
        self.stderr_reader = None

    def start(self):
        self.process = Popen(self.args, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        logging.debug("started process for {} with pid {} args {}".format(
                      type(self), self.process.pid, ','.join(self.args)))

        self.stdout_reader = threading.Thread(target=self.pipe_reader, args=(self.process.stderr, self.handle_stdout))
        self.stdout_reader.daemon = True
        self.stdout_reader.start()

        self.stderr_reader = threading.Thread(target=self.pipe_reader, args=(self.process.stdout, self.handle_stderr))
        self.stderr_reader.daemon = True
        self.stderr_reader.start()

        logging.debug("waiting for {} to start...".format(type(self)))
        wait_for(self.startup_condition)
        logging.debug("{} started".format(type(self)))

    def stop(self):
        if self.process is None:
            return

        logging.debug("stopping process {} with pid {}".format(type(self), self.process.pid))
        self.process.terminate()
        self.process.wait()
        self.process = None

        logging.debug("stopping process output readers...")
        self.stdout_reader.join()
        self.stdout_reader = None
        self.stderr_reader.join()
        self.stderr_reader = None

    def handle_stdout(self, line):
        #print("STDOUT {}\t{}".format(type(self), line.strip()))
         pass

    def handle_stderr(self, line):
        if '[ERROR]' in line:
            print("detected error in subprocess: {}".format(line.strip()))

        #print("STDERR {}\t{}".format(type(self), line.strip()))

    def pipe_reader(self, pipe, callback):
        for line in pipe:
            callback(line.strip())

    def started(self):
        """Returns True if this process has actually started."""
        return True

class EngineProcess(ServerProcess):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine_started = False

    def startup_condition(self):
        return self.engine_started

    def handle_stderr(self, line):
        if 'engine started' in line:
            self.engine_started = True

        ServerProcess.handle_stderr(self, line)

class GUIServer(ServerProcess):
    def __init__(self):
        super().__init__(['python3', 'saq', '-L', 'etc/console_debug_logging.ini', 'start-gui'])
        self.saq_init = 0

    def handle_stderr(self, line):
        if 'SAQ initialized' in line:
            self.saq_init += 1
        
        ServerProcess.handle_stderr(self, line)

    def startup_condition(self):
        return self.saq_init > 1

class ACEBasicTestCase(TestCase):

    def setUp(self):
        saq.DUMP_TRACEBACKS = True
        logging.info("TEST: {}".format(self.id()))
        initialize_test_environment()
        saq.load_configuration()
        open_test_comms()

    def tearDown(self):
        close_test_comms()

        # anything logged at CRITICAL log level will cause the test the fail
        self.assertFalse(memory_log_handler.search(lambda e: e.levelno == logging.CRITICAL))

        saq.DUMP_TRACEBACKS = False

    def wait_for_log_entry(self, *args, **kwargs):
        try:
            return wait_for_log_entry(*args, **kwargs)
        except WaitTimedOutError:
            return False

    def wait_for_condition(self, condition, timeout=5, delay=1):
        """Waits for condition to return True. 
           condition is checked every delay seconds until it return True or timeout seconds have elapsed."""
        time_limit = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
        while True:
            if condition():
                return True

            if datetime.datetime.now() > time_limit:
                raise WaitTimedOutError()

            time.sleep(delay)

    @protect_production
    def reset_brocess(self):
        # clear the brocess db
        with get_db_connection('brocess') as db:
            c = db.cursor()
            c.execute("""DELETE FROM httplog""")
            c.execute("""DELETE FROM smtplog""")
            db.commit()

    @protect_production
    def reset_cloudphish(self):
        # clear cloudphish db
        with get_db_connection('cloudphish') as db:
            c = db.cursor()
            c.execute("""DELETE FROM analysis_results""")
            c.execute("""DELETE FROM content_metadata""")
            c.execute("""DELETE FROM workload""")
            db.commit()

        with get_db_connection('brocess') as db:
            c = db.cursor()
            c.execute("""DELETE FROM httplog""")
            db.commit()

        # clear cloudphish engine and module cache
        for cache_dir in [ saq.CONFIG['cloudphish']['cache_dir'], 
                           saq.CONFIG['analysis_module_cloudphish']['local_cache_dir'] ]:
            if os.path.isdir(cache_dir):
                shutil.rmtree(cache_dir)
                os.makedirs(cache_dir)

    @protect_production
    def reset_correlation(self):
        data_subdir = os.path.join(saq.CONFIG['global']['data_dir'], saq.SAQ_NODE)
        if os.path.isdir(data_subdir):
            try:
                shutil.rmtree(data_subdir)
                os.mkdir(data_subdir)
            except Exception as e:
                logging.error("unable to clear {}: {}".format(data_subdir, e))

        with get_db_connection() as db:
            c = db.cursor()
            c.execute("DELETE FROM alerts")
            c.execute("DELETE FROM workload")
            c.execute("DELETE FROM observables")
            c.execute("DELETE FROM tags")
            c.execute("DELETE FROM profile_points")
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM remediation")
            db.commit()

    @protect_production
    def reset_email_archive(self):
        import socket
        archive_subdir = os.path.join(saq.SAQ_HOME, saq.CONFIG['analysis_module_email_archiver']['archive_dir'], 
                                      socket.gethostname().lower())

        if os.path.exists(archive_subdir):
            try:
                shutil.rmtree(archive_subdir)
                os.mkdir(archive_subdir)
            except Exception as e:
                logging.error("unable to clear {}: {}".format(archive_subdir, e))

        with get_db_connection('email_archive') as db:
            c = db.cursor()
            c.execute("DELETE FROM archive")
            db.commit()

class ACEEngineTestCase(ACEBasicTestCase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # if we create an engine using self.create_engine() then we track it here
        self.tracked_engine = None

        self.server_processes = {} # key = name, value ServerProcess

    def start_gui_server(self):
        self.server_processes['gui'] = GUIServer()
        self.server_processes['gui'].start()

    def start_cloudphish_server(self):
        self.server_processes['cloudphish'] = CloudphishServer()
        self.server_processes['cloudphish'].start()

    def stop_tracked_engine(self):
        if self.tracked_engine:
            try:
                self.tracked_engine.stop()
                self.wait_engine(self.tracked_engine)
            except Exception as e:
                logging.error("unable to stop tracked engine {}: {}".format(self.tracked_engine, e))
                report_exception()
            finally:
                self.tracked_engine = None

    def tearDown(self):
        ACEBasicTestCase.tearDown(self)
        self.stop_tracked_engine()

        for key in self.server_processes.keys():
            self.server_processes[key].stop()

    def execute_engine_test(self, engine):
        try:
            engine.start()
            engine.wait()
        except KeyboardInterrupt:
            engine.stop()
            engine.wait()

    def create_engine(self, cls, *args, **kwargs):
        try:
            self.tracked_engine = cls(*args, **kwargs)
            return self.tracked_engine
        except Exception as e:
            logging.error("unable to create engine {}: {}".format(cls, e))
            report_exception()
            self.fail("unable to create engine {}: {}".format(cls, e))
    
    def start_engine(self, engine):
        try:
            engine.start()
        except Exception as e:
            engine.stop()
            engine.wait()
            self.fail("engine failure: {}".format(e))

    def wait_engine(self, engine):
        try:
            engine.wait()
        except Exception as e:
            engine.controlled_stop()
            engine.wait()
            self.fail("engine failure: {}".format(e))

    def kill_engine(self, engine):
        try:
            engine.stop()
            engine.wait()
        except Exception as e:
            self.fail("engine failure: {}".format(e))

    def disable_all_modules(self):
        """Disables all the modules specified in the configuration file. Requires a @reset_config."""
        for key in saq.CONFIG.keys():
            if key.startswith('analysis_module_'):
                saq.CONFIG[key]['enabled'] = 'no'
        

class CloudphishServer(EngineProcess):
    def __init__(self):
        super().__init__(['python3', 'saq', '-L', 'etc/console_debug_logging.ini', '--start', 'cloudphish'])

class ACEModuleTestCase(ACEEngineTestCase):
    pass

