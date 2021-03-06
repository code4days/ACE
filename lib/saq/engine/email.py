import datetime
import logging
import os
import os.path
import re
import shutil
import socket
import stat
import threading
import time
import uuid

import saq

from saq.analysis import RootAnalysis
from saq.constants import *
from saq.engine import Engine, SSLNetworkServer, MySQLCollectionEngine
from saq.email import normalize_email_address
from saq.error import report_exception

_alert_type_mailbox = 'mailbox'

# backwards compatible support for the older v2 version of the alerts
V2_DETAILS_KEY_CONNECTION_ID = 'connection_id'
V2_DETAILS_KEY_MAIL_FROM = 'envelope mail from'
V2_DETAILS_KEY_RCPT_TO = 'envelope rcpt to'
V2_DETAILS_KEY_FROM = 'from'
V2_DETAILS_KEY_DECODED_FROM = 'decoded_from'
V2_DETAILS_KEY_TO = 'to'
V2_DETAILS_KEY_SUBJECT = 'subject'
V2_DETAILS_KEY_DECODED_SUBJECT = 'decoded_subject'
V2_DETAILS_KEY_HEADER = 'header'

# regular expressions for parsing smtp files generated by bro extraction (see bro/ directory)
REGEX_BRO_SMTP_SOURCE_IPV4 = re.compile(r'^([^:]+):(\d+).*$')
REGEX_BRO_SMTP_MAIL_FROM = re.compile(r'^> MAIL FROM:<([^>]+)>.*$')
REGEX_BRO_SMTP_RCPT_TO = re.compile(r'^> RCPT TO:<([^>]+)>.*$')
REGEX_BRO_SMTP_DATA = re.compile(r'^< DATA 354.*$')

class EmailScanningEngine(SSLNetworkServer, MySQLCollectionEngine, Engine):
    """Processes emails collected by the various email collection engines."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # if set to True then we don't delete the work directories
        self.keep_work_dir = False
        self.hostname = socket.gethostname()

        # thread responsible for consuming the smtp files generated by bro
        self.bro_consumer_thread = None

    @property
    def name(self):
        return 'email_scanner'

    @property
    def bro_smtp_dir(self):
        return os.path.join(saq.SAQ_HOME, self.config['bro_smtp_dir'])

    def initialize_collection(self, *args, **kwargs):
        super().initialize_collection(*args, **kwargs)
        
        # TODO is this the right place to check this?
        if not os.path.isdir(self.bro_smtp_dir):
            try:
                os.makedirs(self.bro_smtp_dir)
            except Exception as e:
                logging.error("unable to create directory {}: {}".format(self.bro_smtp_dir))
            
        self.start_bro_consumer()

    def start_bro_consumer(self):
        self.bro_consumer_thread = threading.Thread(target=self.bro_consumer_loop, name='Bro SMTP Consumer')
        self.bro_consumer_thread.start()

    def bro_consumer_loop(self):
        while not self.collection_shutdown:
            try:
                self.bro_consumer_execute()
            except Exception as e:
                logging.error("unable to consume bro smtp files: {}".format(e))
                self.sleep(60)

    def bro_consumer_execute(self):
        for file_path in os.listdir(self.bro_smtp_dir):
            file_path = os.path.join(self.bro_smtp_dir, file_path)
            if not file_path.endswith('.ready'):
                continue

            #logging.info("MARKER: existing: {}".format(file_path))
            target_file_path = file_path[:len(file_path) - len('.ready')]
            #logging.info("MARKER: processing existing target file {}".format(target_file_path))

            if self.collection_shutdown:
                return

            try:
                self.bro_consumer_process(target_file_path)
            except Exception as e:
                logging.error("unable to process bro smtp file {}: {}".format(target_file_path, e))
                report_exception()

                # TODO copy the file for analysis

            finally:

                # delete the processed files
                try:
                    os.remove(target_file_path)
                except Exception as e:
                    logging.error("unable to delete {}: {}".format(target_file_path, e))

                try:
                    os.remove(file_path)
                except Exception as e:
                    logging.error("unable to delete {}: {}".format(file_path, e))

        time.sleep(self.collection_frequency)

    def bro_consumer_process(self, target_file_path):
        logging.info("processing bro smtp file {}".format(target_file_path))

        with open(target_file_path, 'r') as fp:
            source_ipv4 = None
            source_port = None
            envelope_from = None
            envelope_to = []

            root = RootAnalysis()
            root.uuid = str(uuid.uuid4())
            root.storage_dir = os.path.join(self.collection_dir, root.uuid[0:3], root.uuid)
            root.initialize_storage()

            root.tool = 'ACE - Bro SMTP Scanner'
            root.tool_instance = self.hostname
            root.alert_type = 'mailbox'
            root.description = 'BRO SMTP Scanner Detection - ' 
            root.event_time = datetime.datetime.now() 
            root.details = { }

            # the first line of the file has the source IP address of the smtp connection
            # in the following format: 172.16.139.143:38668/tcp

            line = fp.readline()
            m = REGEX_BRO_SMTP_SOURCE_IPV4.match(line)

            if not m:
                logging.error("unable to parse source address from {} ({})".format(target_file_path, line.strip()))
            else:
                source_ipv4 = m.group(1)
                source_port = m.group(2)

                logging.debug("got source ipv4 {} port {} for {}".format(source_ipv4, source_port, target_file_path))

            # the second line is the time (in epoch UTC) that bro received the file
            line = fp.readline()
            root.event_time = datetime.datetime.utcfromtimestamp(int(line.strip()))
            logging.debug("got event time {} for {}".format(root.event_time, target_file_path))

            STATE_SMTP = 1
            STATE_DATA = 2

            state = STATE_SMTP
            rfc822_path = None
            rfc822_fp = None

            # smtp is pretty much line oriented
            for line in fp:
                if state == STATE_SMTP:
                    m = REGEX_BRO_SMTP_MAIL_FROM.match(line)
                    if m:
                        envelope_from = m.group(1)
                        logging.debug("got envelope_from {} for {}".format(envelope_from, target_file_path))
                        continue

                    m = REGEX_BRO_SMTP_RCPT_TO.match(line)
                    if m:
                        envelope_to.append(m.group(1))
                        logging.debug("got envelope_to {} for {}".format(envelope_to, target_file_path))
                        continue

                    m = REGEX_BRO_SMTP_DATA.match(line)
                    if m:
                        state = STATE_DATA
                        rfc822_path = os.path.join(root.storage_dir, 'email.rfc822')
                        rfc822_fp = open(rfc822_path, 'w')
                        logging.debug("created {} for {}".format(rfc822_path, target_file_path))
                        continue

                    # any other command we skip
                    continue

                # otherwise we're reading DATA and looking for the end of that
                if line.strip() == ('> . .'):
                    rfc822_fp.close()

                    logging.info("finished parsing {} from {}".format(rfc822_path, target_file_path))

                    # submit this for analysis...
                    email_file = root.add_observable(F_FILE, os.path.relpath(rfc822_path, start=root.storage_dir))
                    if email_file:
                        email_file.add_directive(DIRECTIVE_ORIGINAL_EMAIL)
                        # we don't scan the email as a whole because of all the random base64 data
                        # that randomly matches various indicators from crits
                        # instead we rely on all the extraction that we do and scan the output of those processes
                        email_file.add_directive(DIRECTIVE_NO_SCAN)
                        # make sure we archive it
                        email_file.add_directive(DIRECTIVE_ARCHIVE)

                    try:
                        root.save()
                    except Exception as e:
                        logging.error("unable to save {}: {}".format(root, e))
                        continue

                    try:
                        self.add_sql_work_item(root.storage_dir)
                    except Exception as e:
                        logging.error("unable to submit work item for {}: {}".format(root.storage_dir, e))
                        report_exception()
                        continue

                    rfc822_fp = None
                    root = None
                    source_ipv4 = None
                    source_port = None
                    envelope_from = None
                    envelope_to = []

                    state = STATE_SMTP
                    continue

                rfc822_fp.write(line)
                continue

    def handle_network_item(self, path):

        # the file that is submitted here is the FULL PATH to the journaled email
        logging.info("received network item {}".format(path))

        root = RootAnalysis()
        root.uuid = str(uuid.uuid4())
        root.storage_dir = os.path.join(self.collection_dir, root.uuid[0:3], root.uuid)
        root.initialize_storage()

        root.tool = 'ACE - Mailbox Scanner'
        root.tool_instance = self.hostname
        root.alert_type = 'mailbox'
        root.description = 'ACE Mailbox Scanner Detection - ' # not sure how to deal with this yet...
        root.event_time = datetime.datetime.now()
        root.details = { }

        # move the file we just got over to the new storage directory
        dest_path = os.path.join(root.storage_dir, 'email.rfc822')
        try:
            shutil.move(path, dest_path)
            logging.debug("moved {} to {}".format(path, dest_path))
        except Exception as e:
            logging.error("unable to move {} to {}: {}".format(path, dest_path, e))
            report_exception()
            return

        # make sure the permissions are correct

        try:
            os.chmod(dest_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except Exception as e:
            logging.error("unable to chmod {}: {}".format(dest_path, e))

        email_file = root.add_observable(F_FILE, os.path.relpath(dest_path, start=root.storage_dir))
        if email_file:
            email_file.add_directive(DIRECTIVE_ORIGINAL_EMAIL)
            # we don't scan the email as a whole because of all the random base64 data
            # that randomly matches various indicators from crits
            # instead we rely on all the extraction that we do and scan the output of those processes
            email_file.add_directive(DIRECTIVE_NO_SCAN)
            # make sure we archive it
            email_file.add_directive(DIRECTIVE_ARCHIVE)

        try:
            root.save()
        except Exception as e:
            logging.error("unable to save {}: {}".format(root, e))
            return

        self.add_sql_work_item(root.storage_dir)

    def process(self, path):

        self.root = RootAnalysis()
        self.root.storage_dir = path

        try:
            self.root.load()
        except Exception as e:
            logging.error("unable to load {}: {}".format(self.root, e))
            return

        # now we move this path into the work directory
        dest_dir = os.path.join(self.work_dir, self.root.uuid[0:3], self.root.uuid)
        if os.path.exists(dest_dir):
            logging.error("work directory {} already exists!?".format(dest_dir))
            return

        try:
            logging.debug("moving {} to {} for work".format(self.root.storage_dir, dest_dir))
            shutil.move(self.root.storage_dir, dest_dir)
        except Exception as e:
            logging.error("unable to move {} to {}: {}".format(self.root.storage_dir, dest_dir, e))
            return

        self.root.storage_dir = dest_dir

        try:
            self.analyze(self.root)
        except Exception as e:
            logging.error("analysis failed for {}: {}".format(path, e))
            report_exception()
    
    def post_analysis(self, root):
        # there are currently only two sources of emails
        # brotex and mailbox clients
        try:
            if root.alert_type == _alert_type_mailbox:
                self.post_mailbox_analysis(root)
            else:
                self.post_brotex_analysis(root)
        except Exception as e:
            logging.error("unable to execute post analysis for {}: {}".format(self, e))
            report_exception()

        # if we've alerted then we don't need to complete any outstanding delayed analysis
        # XXX actually this will never happen because you don't come into this function
        # XXX if there is outstanding delayed analysis
        if root.alerted:
            self.cancel_analysis()
        else:
            # any outstanding analysis left?
            if root.delayed:
                logging.info("{} has delayed analysis -- waiting for cleanup...".format(root))
                return

    def post_mailbox_analysis(self, root):
        import saq.modules.email

        # there should be a single file observable for the alert
        email_file = None
        for o in root.observables:
            if o.type == F_FILE and o.has_directive(DIRECTIVE_ORIGINAL_EMAIL):
                email_file = o

        # 10/27/2017
        # the exception to that would be the new office365 detection reports
        is_office365_report = False
        for o365_analysis in root.get_analysis_by_type(saq.modules.email.Office365BlockAnalysis):
            message_id = o365_analysis.get_observables_by_type(F_MESSAGE_ID)
            if message_id:
                message_id = message_id[0]
                #logging.info("MARKER: message_id = {}".format(message_id))
                message_id_analysis = message_id.get_analysis(saq.modules.email.MessageIDAnalysis)
                if message_id_analysis:
                    encrypted_o365_report = message_id_analysis.get_observables_by_type(F_FILE)
                    #logging.info("MARKER: encrypted_o365_report = {}".format(encrypted_o365_report))
                    if encrypted_o365_report:
                        encrypted_o365_report = encrypted_o365_report[0]
                        encrypted_email_analysis = encrypted_o365_report.get_analysis(saq.modules.email.EncryptedArchiveAnalysis)
                        if encrypted_email_analysis:
                            o365_report = encrypted_email_analysis.get_observables_by_type(F_FILE)
                            #logging.info("MARKER: o365_report = {}".format(o365_report))
                            if o365_report:
                                root.alert_type = 'o365'
                                root.description = 'Office365 Blocked Email Report - '
                                is_office365_report = True
                                email_file = o365_report[0]
                                logging.info("found office365 block report {}".format(email_file))

        if email_file is None:
            logging.error("cannot find original email file for {}".format(root))
            return

        # was the email whitelisted?
        if email_file.has_tag('whitelisted'):
            logging.info("email for {} was whitelisted".format(self.root))
            return

        # do we need to generate an alert for this email?
        if not self.should_alert(root):
            return

        # get the email analysis for this analysis
        analysis = email_file.get_analysis(saq.modules.email.EmailAnalysis)
        if not analysis:
            logging.error("cannot find email analysis for {} in {}".format(email_file, root))
            return

        # this seems to happen every now and then, we'll catch it here and hopefully refactor this out
        if not analysis.email:
            logging.warning("email analysis for {} does not have email details".format(email_file))
            root.description = 'Unparsable Email'
        else:
            if analysis.decoded_subject:
                root.description += '{} '.format(analysis.decoded_subject)
            elif analysis.subject:
                root.description += '{} '.format(analysis.subject)
            else:
                root.description += '(no subject) '
                if analysis.env_mail_from:
                    root.description += 'From {} '.format(normalize_email_address(analysis.env_mail_from))
                elif analysis.mail_from:
                    root.description += 'From {} '.format(normalize_email_address(analysis.mail_from))
                if analysis.env_rcpt_to:
                    if len(analysis.env_rcpt_to) == 1:
                        root.description += 'To {} '.format(analysis.env_rcpt_to[0])
                    else:
                        root.description += 'To ({} recipients) '.format(len(analysis.env_rcpt_to))
                elif analysis.mail_to:
                    if isinstance(analysis.mail_to, list): # XXX I think this *has* to be a list
                        if len(analysis.mail_to) == 1:
                            root.description += 'To {} '.format(analysis.mail_to[0])
                        else:
                            root.description += 'To ({} recipients) '.format(len(analysis.mail_to))
                    else:
                        root.description += 'To {} '.format(analysis.mail_to)

        root.details.update(analysis.details)
        root.submit()

    def post_brotex_analysis(self, root):
        import saq.modules.email

        if not self.should_alert(root):
            return

        # provide support for the older v2 brotex alerts
        if saq.modules.email.KEY_ENV_MAIL_FROM in root.details[saq.modules.email.KEY_EMAIL]:
            root.details[V2_DETAILS_KEY_MAIL_FROM] = root.details[saq.modules.email.KEY_EMAIL]\
                                                                   [saq.modules.email.KEY_ENV_MAIL_FROM]
        if saq.modules.email.KEY_ENV_RCPT_TO in root.details[saq.modules.email.KEY_EMAIL]:
            root.details[V2_DETAILS_KEY_RCPT_TO] = root.details[saq.modules.email.KEY_EMAIL]\
                                                                 [saq.modules.email.KEY_ENV_RCPT_TO]
        if saq.modules.email.KEY_FROM in root.details[saq.modules.email.KEY_EMAIL]:
            root.details[V2_DETAILS_KEY_FROM] = root.details[saq.modules.email.KEY_EMAIL]\
                                                              [saq.modules.email.KEY_FROM]
        if saq.modules.email.KEY_TO in root.details[saq.modules.email.KEY_EMAIL]:
            _ = root.details[saq.modules.email.KEY_EMAIL][saq.modules.email.KEY_TO]
            root.details[V2_DETAILS_KEY_TO] = root.details[saq.modules.email.KEY_EMAIL]\
                                                            [saq.modules.email.KEY_TO]
        if saq.modules.email.KEY_SUBJECT in root.details[saq.modules.email.KEY_EMAIL]:
            root.details[V2_DETAILS_KEY_SUBJECT] = root.details[saq.modules.email.KEY_EMAIL]\
                                                                 [saq.modules.email.KEY_SUBJECT]
        if saq.modules.email.KEY_DECODED_SUBJECT in root.details[saq.modules.email.KEY_EMAIL]:
            root.details[V2_DETAILS_KEY_DECODED_SUBJECT] = root.details[saq.modules.email.KEY_EMAIL]\
                                                                         [saq.modules.email.KEY_DECODED_SUBJECT]

        if saq.modules.email.KEY_HEADERS in root.details[saq.modules.email.KEY_EMAIL]:
            _buffer = []
            for k, v in root.details[saq.modules.email.KEY_EMAIL][saq.modules.email.KEY_HEADERS]:
                _buffer.append('{}: {}'.format(k, bytes(v, 'utf-8').decode('unicode_escape')))
            root.details[V2_DETAILS_KEY_HEADER] = '\r\n'.join(_buffer)

        if 'connection_id' in root.details:
            root.details[V2_DETAILS_KEY_CONNECTION_ID] = root.details['connection_id']

        root.submit()
    
    def cleanup(self, work_item):
        if not self.root:
            return

        if self.root.delayed:
            return

        if not self.keep_work_dir:
            self.root.delete()

    def get_tracking_information(self, root):
        from saq.modules.email import EmailAnalysis

        email_file = None
        for o in root.observables:
            if o.type == F_FILE and o.has_directive(DIRECTIVE_ORIGINAL_EMAIL):
                email_file = o

        if not email_file:
            return {}

        analysis = email_file.get_analysis(EmailAnalysis)
        if not analysis:
            return {}

        # just use the analysis details as the tracking information
        details = analysis.details.copy()
        # some things we don't need
        if 'email' in details:
            if 'headers' in details['email']:
                del details['email']['headers']

        return details
