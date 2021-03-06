# vim: sw=4:ts=4:et

import datetime
import logging
import shutil
import threading
import uuid

from contextlib import closing, contextmanager

import saq
import saq.analysis
import saq.constants

from saq.analysis import RootAnalysis
from saq.error import report_exception
from saq.lock import LockableObject, lock_expired
from saq.performance import track_execution_time

import businesstime
import pymysql

from businesstime.holidays import Holidays

# this provides a way for a process + thread to re-use the same database connection
_global_db_cache = {} # key = current_process_id:current_thread_id:config_name, value = database connection
_global_db_cache_lock = threading.RLock()

# used to determine if cached db connections are enabled for this process + thread
_use_cache_flags = set() # key = current_process_id:current_thread_id
_use_cache_flags_lock = threading.RLock()

def _get_cache_identifier():
    """Returns the key for _use_cache_flags"""
    return '{}:{}'.format(os.getpid(), threading.get_ident())

def _get_cached_db_identifier(name):
    """Returns the key for _global_db_cache"""
    return '{}:{}:{}'.format(str(os.getpid()), str(threading.get_ident()), name)

def enable_cached_db_connections():
    """Enables cached database connections for this process and thread."""
    with _use_cache_flags_lock:
        _use_cache_flags.add(_get_cache_identifier())
    logging.debug("using cached database connections for {}".format(_get_cache_identifier()))

def disable_cached_db_connections():
    """Disables cached database connections for this process and thread.
       This function also removes any existing database connections that have been cached."""
    with _global_db_cache_lock:
        keys = list(_global_db_cache.keys())
        for key in keys:
            if key.startswith(_get_cache_identifier()):
                name = key.split(':')[2]
                logging.debug("requesting release of {} for {}".format(name, _get_cache_identifier()))
                release_cached_db_connection(name)

    with _use_cache_flags_lock:
        _use_cache_flags.remove(_get_cache_identifier())

def _cached_db_connections_enabled():
    """Returns True if cached database connections are enabled for this process and thread."""
    with _use_cache_flags_lock:
        return _get_cache_identifier() in _use_cache_flags

def _get_cached_db_connection(name='ace'):
    """Returns the database connection by the given name.  Defaults to the ACE db config."""
    config_section = 'database_{}'.format(name)

    if config_section not in saq.CONFIG:
        raise ValueError("invalid database {}".format(name))

    try:
        db_identifier = _get_cached_db_identifier(name)
        with _global_db_cache_lock:
            logging.debug("aquiring existing cached database connection {}".format(db_identifier))
            db = _global_db_cache[db_identifier]

        try:
            db.rollback()
            #logging.debug("acquired cached database connection to {}".format(name))
            return db

        except Exception as e:
            logging.debug("possibly lost cached connection to database {}: {} ({})".format(name, e, type(e)))
            try:
                db.close()
            except Exception as e:
                logging.debug("unable to close cached database connection to {}: {}".format(name, e))

            with _global_db_cache_lock:
                del _global_db_cache[db_identifier]

            return _get_db_connection(name)

    except KeyError:

        try:
            logging.debug("opening new cached database connection to {}".format(name))
            _section = saq.CONFIG[config_section]
            db = pymysql.connect(host=_section['hostname'] if 'hostname' in _section else None,
                                 unix_socket=_section['unix_socket'] if 'unix_socket' in _section else None,
                                 db=_section['database'],
                                 user=_section['username'],
                                 passwd=_section['password'],
                                 charset='utf8')

            with _global_db_cache_lock:
                _global_db_cache[db_identifier] = db

            logging.debug("opened cached database connection {}".format(db_identifier))
            return db

        except Exception as e:
            logging.error("unable to connect to database {}: {}".format(name, e))
            report_exception()
            raise e

def release_cached_db_connection(name='ace'):

    # make sure this process + thread is using cached connections
    if not _cached_db_connections_enabled():
        return

    db_identifier = _get_cached_db_identifier(name)

    try:
        with _global_db_cache_lock:
            db = _global_db_cache[db_identifier]

        try:
            db.close()
        except Exception as e:
            logging.debug("unable to close database connect to {}: {}".format(name, e))

        with _global_db_cache_lock:
            del _global_db_cache[db_identifier]

        logging.debug("released cached database connection {}".format(db_identifier))

    except KeyError:
        pass

def _get_db_connection(name='ace'):
    """Returns the database connection by the given name.  Defaults to the ACE db config under [mysql]."""

    if _cached_db_connections_enabled():
        return _get_cached_db_connection(name)

    config_section = 'ace'
    if name:
        config_section = 'database_{}'.format(name)

    if config_section not in saq.CONFIG:
        raise ValueError("invalid database {}".format(name))

    _section = saq.CONFIG[config_section]
    logging.debug("opening database connection {} host {} db {}".format(name, _section['hostname'], _section['database']))
    return pymysql.connect(host=_section['hostname'] if 'hostname' in _section else None,
                           port=3306 if 'port' not in _section else _section.getint('port'),
                           unix_socket=_section['unix_socket'] if 'unix_socket' in _section else None,
                           db=_section['database'],
                           user=_section['username'],
                           passwd=_section['password'],
                           charset='utf8')

@contextmanager
def get_db_connection(*args, **kwargs):
    if _cached_db_connections_enabled():
        db = _get_cached_db_connection(*args, **kwargs)
    else:
        db = _get_db_connection(*args, **kwargs)

    try:
        yield db
    except Exception as e:
        try:
            if _cached_db_connections_enabled():
                db.rollback()
            else:
                db.close()
        except Exception as failure_error:
            logging.error("unable to roll back or close transaction: {}".format(failure_error))
            report_exception()
            raise e

        raise e

def execute_with_retry(cursor, sql, params, attempts=2):
    """Executes the given SQL (and params) against the given cursor with re-attempts up to N times (defaults to 2) on deadlock detection."""
    count = 1
    while True:
        try:
            cursor.execute(sql, params)
            break
        except pymysql.err.InternalError as e:
            # see http://stackoverflow.com/questions/25026244/how-to-get-the-mysql-type-of-error-with-pymysql
            # to explain e.args[0]
            if (e.args[0] == 1213 or e.args[0] == 1205) and count < attempts:
                logging.debug("deadlock detected -- trying again...")
                count += 1
                continue
            else:
                raise e

# new school database connections
import logging
import os.path
from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, DATE, text, create_engine, Text, Enum
from sqlalchemy.dialects.mysql import BOOLEAN
from sqlalchemy.orm import sessionmaker, relationship, reconstructor, backref
from sqlalchemy.orm.exc import NoResultFound, DetachedInstanceError
from sqlalchemy.orm.session import Session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import and_, or_
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

DatabaseSession = None
Base = declarative_base()

class User(UserMixin, Base):

    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True)
    email = Column(String(64), unique=True, index=True)
    password_hash = Column(String(128))
    omniscience = Column(Integer, nullable=False, default=0)

    def __str__(self):
        return self.username

    @property
    def password(self):
        raise AttributeError('password is not a readable attribute')
    
    @password.setter
    def password(self, value):
        self.password_hash = generate_password_hash(value)

    def verify_password(self, value):
        return check_password_hash(self.password_hash, value)

class ACEAlertLock(LockableObject):
    """An implementation of locking for Alerts."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.acquired_lock_id = None

    @property
    def lock_identifier(self):
        """Returns what is the unique id for this lock."""
        return self.acquired_lock_id

    def lock(self):
        assert hasattr(self, 'id')

        if not self.id:
            logging.error("called lock() on {} when id was None".format(self))
            return False

        # are we already locked?
        if self.acquired_lock_id:
            # has this lock expired?
            if self.is_locked():
                logging.warning("called lock() on already locked {}".format(self))
                return False

            # if the lock has expired then we forget about our acquired lock id
            self.acquired_lock_id = None

        # what we do here is UPDATE table SET lock_owner = us, lock_id = lock_id, lock-time = NOW() WHERE lock_owner IS NONE
        # we use a generated "lock id" so we know how actually ended up getting the lock
        # we are relying on the ATOMicity of the database to ensure only one request actually obtains the lock
        # we do that by using a WHERE clause on the fields we are updating
        acquired_lock_id = str(uuid.uuid4())
        logging.debug("using lock_id {} for {}".format(acquired_lock_id, self))

        with get_db_connection() as db:
            c = db.cursor()
            c.execute("""
                      UPDATE alerts 
                      SET lock_owner = %s, lock_id = %s, lock_time = NOW() 
                      WHERE id = %s AND lock_owner IS NULL""", (
                      saq.SAQ_NODE,
                      acquired_lock_id,
                      self.id))

            db.commit()

            # did we get the lock?
            c.execute("""SELECT lock_id, lock_time FROM alerts WHERE id = %s""", ( self.id, ))
            try:
                row = c.fetchone()
            except Exception as e:
                logging.warning("unable to get alert row {} for locking: {}".format(self.id))
                return False

            current_lock_id, current_lock_time = row
            if current_lock_id == acquired_lock_id:
                logging.debug("acquired lock on {}".format(self))
                self.acquired_lock_id = acquired_lock_id
                return True

            # we were not able to acquire the lock AND there is no existing lock
            if current_lock_time is None:
                logging.warning("unable to acquire lock on {} and lock_time is not set".format(self.id))
                return False

            # unable to acquire the lock but has the existing lock expired?
            if lock_expired(current_lock_time):
                logging.warning("detected expired lock on {}".format(self))

                c.execute("""
                          UPDATE alerts 
                          SET lock_owner = %s, lock_id = %s, lock_time = NOW() 
                          WHERE id = %s AND lock_id = %s""", (
                          saq.SAQ_NODE,
                          acquired_lock_id,
                          self.id,
                          current_lock_id))

                db.commit()

                # did we get the lock?
                c.execute("""SELECT lock_id, lock_time FROM alerts WHERE id = %s""", ( self.id, ))
                try:
                    row = c.fetchone()
                except Exception as e:
                    logging.warning("unable to get alert row {} for locking: {}".format(self.id))
                    return False

                current_lock_id, current_lock_time = row
                if current_lock_id == acquired_lock_id:
                    logging.debug("acquired lock on {}".format(self))
                    self.acquired_lock_id = acquired_lock_id
                    return True

                logging.warning("unable to acquire expired lock {} (another process grabbed it?)".format(self))
                return False
            
            logging.debug("{} is already locked".format(self))
            return False

    def unlock(self):
        assert hasattr(self, 'id')

        if not self.id:
            logging.error("called unlock() on {} when id was None".format(self))
            return False

        # are we not locked?
        if not self.acquired_lock_id:
            logging.warning("called unlock() on already unlocked {}".format(self))
            self.acquired_lock_id = None
            return False

        # go ahead and try to unlock using our own ID in the where clause
        # this will ensure we do not unlock if we do not own it

        with get_db_connection() as db:
            c = db.cursor()
            c.execute("""
                      UPDATE alerts 
                      SET lock_owner = NULL, lock_id = NULL, lock_time = NULL
                      WHERE id = %s AND lock_id = %s""", (
                      self.id,
                      self.acquired_lock_id))

            if c.rowcount == 0:
                logging.warning("unable to unlock {} (expired?)".format(self))
                self.acquired_lock_id = None
                return False

            db.commit()
            logging.debug("unlocked lock {} on {}".format(self.acquired_lock_id, self))
            self.acquired_lock_id = None
            return True

    def has_current_lock(self):
        """Returns True if the lock is currently held, False otherwise."""
        return self.acquired_lock_id is not None

    def is_locked(self):
        assert hasattr(self, 'id')

        if not self.id:
            logging.error("called lock() on {} when id was None".format(self))
            return False

        #if self.acquired_lock_id:
            #logging.warning("called lock() on already locked {}".format(self))
            #return True

        with get_db_connection() as db:
            c = db.cursor()
            # is this alert locked?
            c.execute("""SELECT lock_id, lock_time FROM alerts WHERE id = %s""", ( self.id, ))
            try:
                row = c.fetchone()
            except Exception as e:
                logging.warning("unable to get alert row {} for locking: {}".format(self.id))
                return False

        current_lock_id, current_lock_time = row
        if current_lock_id:
            # has the existing lock expired?
            if lock_expired(current_lock_time):
                logging.warning("detected expired lock on {}".format(self))
                return False
            return True
        return False

    def refresh_lock(self):
        assert hasattr(self, 'id')

        if not self.id:
            logging.error("called refresh_lock() on {} when id was None".format(self))
            return False

        if not self.acquired_lock_id:
            logging.warning("called refresh_lock() on unlocked {}".format(self))
            return False

        with get_db_connection() as db:
            transaction_id = str(uuid.uuid4())
            logging.debug("refreshing lock {} for alert {} with transaction_id {}".format(
                          self.acquired_lock_id, self.id, transaction_id))
            c = db.cursor()
            c.execute("""
                      UPDATE alerts 
                      SET lock_time = NOW(),
                      lock_transaction_id = %s
                      WHERE id = %s AND lock_id = %s""", (
                      transaction_id,
                      self.id,
                      self.acquired_lock_id))

            if c.rowcount == 0:
                logging.warning("unable to refresh {} (expired?)".format(self))
                self.acquired_lock_id = None
                return False

            db.commit()
            return True

    def transfer_locks_to(self, lockable):
        assert isinstance(lockable, ACEAlertLock)
        lockable.acquired_lock_id = self.acquired_lock_id
        logging.debug("transfered lock_id {} to {}".format(self.acquired_lock_id, lockable))

    def create_lock_proxy(self):
        proxy = ACEAlertLock()
        proxy.id = self.id
        return proxy

class Campaign(Base):
    __tablename__ = 'campaign'
    id = Column(Integer, nullable=False, primary_key=True)
    name = Column(String(128), nullable=False)

class Event(Base):

    __tablename__ = 'events'

    id = Column(Integer, nullable=False, primary_key=True)
    creation_date = Column(DATE, nullable=False)
    name = Column(String(128), nullable=False)
    status = Column(Enum('OPEN','CLOSED','IGNORE'), nullable=False)
    remediation = Column(Enum('not remediated','cleaned with antivirus','cleaned manually','reimaged','credentials reset','removed from mailbox','NA'), nullable=False)
    comment = Column(Text)
    vector = Column(Enum('corporate email','webmail','usb','website','unknown'), nullable=False)
    prevention_tool = Column(Enum('response team','ips','fw','proxy','antivirus','email filter','application whitelisting','user'), nullable=False)
    campaign_id = Column(Integer, ForeignKey('campaign.id'), nullable=False)
    campaign = relationship('saq.database.Campaign', foreign_keys=[campaign_id])
    type = Column(Enum('phish','recon','host compromise','credential compromise','web browsing'), nullable=False)
    malware = relationship("saq.database.MalwareMapping", passive_deletes=True, passive_updates=True)
    alert_mappings = relationship("saq.database.EventMapping", passive_deletes=True, passive_updates=True)
    companies = relationship("saq.database.CompanyMapping", passive_deletes=True, passive_updates=True)

    @property
    def malware_names(self):
        names = []
        for mal in self.malware:
            names.append(mal.name)
        return names

    @property
    def company_names(self):
        names = []
        for company in self.companies:
            names.append(company.name)
        return names

    @property
    def commentf(self):
        if self.comment is None:
            return ""
        return self.comment

    @property
    def threats(self):
        threats = {}
        for mal in self.malware:
            for threat in mal.threats:
                threats[threat.type] = True
        return threats.keys()

    @property
    def disposition(self):
        dis_rank = {
                'FALSE_POSITIVE':0,
                'IGNORE':-1,
                'UNKNOWN':1,
                'REVIEWED':2,
                'POLICY_VIOLATION':3,
                'GRAYWARE':3,
                'RECONNAISSANCE':4,
                'WEAPONIZATION':5,
                'DELIVERY':6,
                'EXPLOITATION':7,
                'INSTALLATION':8,
                'COMMAND_AND_CONTROL':9,
                'EXFIL':10,
                'DAMAGE':11}
        disposition = None
        for alert_mapping in self.alert_mappings:
            if disposition is None or dis_rank[alert_mapping.alert.disposition] > dis_rank[disposition]:
                disposition = alert_mapping.alert.disposition
        return disposition

    @property
    def disposition_rank(self):
        if self.disposition is None:
            return -2

        dis_rank = {
                'FALSE_POSITIVE':0,
                'IGNORE':-1,
                'UNKNOWN':1,
                'REVIEWED':2,
                'POLICY_VIOLATION':3,
                'GRAYWARE':3,
                'RECONNAISSANCE':4,
                'WEAPONIZATION':5,
                'DELIVERY':6,
                'EXPLOITATION':7,
                'INSTALLATION':8,
                'COMMAND_AND_CONTROL':9,
                'EXFIL':10,
                'DAMAGE':11}
        return dis_rank[self.disposition]

    @property
    def sorted_tags(self):
        tags = {}
        for alert_mapping in self.alert_mappings:
            for tag_mapping in alert_mapping.alert.tag_mappings:
                tags[tag_mapping.tag.name] = tag_mapping.tag
        return sorted([x for x in tags.values()], key=lambda x: (-x.score, x.name.lower()))

    @property
    def wiki(self):
        domain = saq.CONFIG['mediawiki']['domain']
        date = self.creation_date.strftime("%Y%m%d").replace(' ', '+')
        name = self.name.replace(' ', '+')
        return "{}display/integral/{}+{}".format(domain, date, name)

class EventMapping(Base):

    __tablename__ = 'event_mapping'

    event_id = Column(Integer, ForeignKey('events.id'), primary_key=True)
    alert_id = Column(Integer, ForeignKey('alerts.id'), primary_key=True)

    alert = relationship('saq.database.Alert', backref='event_mapping')
    event = relationship('saq.database.Event', backref='event_mapping')

class Company(Base):

    __tablename__ = 'company'

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True)

class CompanyMapping(Base):

    __tablename__ = 'company_mapping'

    event_id = Column(Integer, ForeignKey('events.id'), primary_key=True)
    company_id = Column(Integer, ForeignKey('company.id'), primary_key=True)
    company = relationship("saq.database.Company")

    @property
    def name(self):
        return self.company.name

class Malware(Base):

    __tablename__ = 'malware'

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True)
    threats = relationship("saq.database.Threat", passive_deletes=True, passive_updates=True)

class MalwareMapping(Base):

    __tablename__ = 'malware_mapping'

    event_id = Column(Integer, ForeignKey('events.id'), primary_key=True)
    malware_id = Column(Integer, ForeignKey('malware.id'), primary_key=True)
    malware = relationship("saq.database.Malware")

    @property
    def threats(self):
        return self.malware.threats

    @property
    def name(self):
        return self.malware.name

class Threat(Base):

    __tablename__ = 'malware_threat_mapping'

    malware_id = Column(Integer, ForeignKey('malware.id'), primary_key=True)
    type = Column(Enum('UNKNOWN','KEYLOGGER','INFOSTEALER','DOWNLOADER','BOTNET','RAT','RANSOMWARE','ROOTKIT','CLICK_FRAUD'), primary_key=True, nullable=False)

    def __str__(self):
        return self.type

class SiteHolidays(Holidays):
    rules = [
        dict(name="New Year's Day", month=1, day=1),
        #dict(name="Birthday of Martin Luther King, Jr.", month=1, weekday=0, week=3),
        #dict(name="Washington's Birthday", month=2, weekday=0, week=3),
        dict(name="Memorial Day", month=5, weekday=0, week=-1),
        dict(name="Independence Day", month=7, day=4),
        dict(name="Labor Day", month=9, weekday=0, week=1),
        #dict(name="Columbus Day", month=10, weekday=0, week=2),
        #dict(name="Veterans Day", month=11, day=11),
        dict(name="Thanksgiving Day", month=11, weekday=3, week=4),
        dict(name="Day After Thanksgiving Day", month=11, weekday=4, week=4),
        dict(name="Chistmas Eve", month=12, day=24),
        dict(name="Chistmas Day", month=12, day=25),
    ]

    def _day_rule_matches(self, rule, dt):
        """
        Day-of-month-specific US federal holidays that fall on Sat or Sun are
        observed on Fri or Mon respectively. Note that this method considers
        both the actual holiday and the day of observance to be holidays.
        """
        if dt.weekday() == 4:
            sat = dt + datetime.timedelta(days=1)
            if super(SiteHolidays, self)._day_rule_matches(rule, sat):
                return True
        elif dt.weekday() == 0:
            sun = dt - datetime.timedelta(days=1)
            if super(SiteHolidays, self)._day_rule_matches(rule, sun):
                return True
        return super(SiteHolidays, self)._day_rule_matches(rule, dt)

_bt = businesstime.BusinessTime(business_hours=(datetime.time(6), datetime.time(18)), holidays=SiteHolidays())

class Alert(ACEAlertLock, RootAnalysis, Base):

    def _initialize(self):
        # keep track of what Tag and Observable objects we add as we analyze
        self._tracked_tags = [] # of saq.analysis.Tag
        self._tracked_observables = [] # of saq.analysis.Observable
        self._synced_tags = set() # of Tag.name
        self._synced_observables = set() # of '{}:{}'.format(observable.type, observable.value)
        self.add_event_listener(saq.constants.EVENT_GLOBAL_TAG_ADDED, self._handle_tag_added)
        self.add_event_listener(saq.constants.EVENT_GLOBAL_OBSERVABLE_ADDED, self._handle_observable_added)

        # the ID we're using to lock this alert
        self.acquired_lock_id = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initialize()

    @reconstructor
    def init_on_load(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initialize()

    __tablename__ = 'alerts'

    id = Column(
        Integer, 
        primary_key=True)

    company_id = Column(
        Integer,
        ForeignKey('company.id'),
        nullable=True)

    company = relationship('saq.database.Company', foreign_keys=[company_id])

    uuid = Column(
        String(36), 
        unique=True, 
        nullable=False)

    location = Column(
        String(253),
        unique=False,
        nullable=False)

    storage_dir = Column(
        String(512), 
        unique=True, 
        nullable=False)

    insert_date = Column(
        TIMESTAMP, 
        nullable=False, 
        server_default=text('CURRENT_TIMESTAMP'))

    @property
    def sla(self):
        """Returns the correct SLA for this alert, or None if SLA is disabled for this alert."""
        if hasattr(self, '_sla_settings'):
            return getattr(self, '_sla_settings')

        target_sla = None

        # find the SLA setting that matches this alert
        try:
            for sla in saq.OTHER_SLA_SETTINGS:
                #logging.info("MARKER: {} {} {}".format(self.uuid, getattr(self, sla._property), sla._value))
                if str(getattr(self, sla._property)) == str(sla._value):
                    logging.debug("alert {} matches property {} value {} for SLA {}".format(
                                   self, sla._property, sla._value, sla.name))
                    target_sla = sla
                    break

            # if nothing matched then just use global sla
            if target_sla is None:
                logging.debug("alert {} uses global SLA settings".format(self))
                target_sla = saq.GLOBAL_SLA_SETTINGS

        except Exception as e:
            logging.error("unable to get SLA: {}".format(e))

        setattr(self, '_sla_settings', target_sla)
        return target_sla

    @property
    def business_time(self):
        """Returns a time delta that represents how old this alert is in business days and hours."""
        # remember that 1 day == 8 hours
        if hasattr(self, '_business_time'):
            return getattr(self, '_business_time')

        result = _bt.businesstimedelta(self.insert_date, datetime.datetime.now())
        setattr(self, '_business_time', result)
        return result

    @property
    def business_time_str(self):
        """Returns self.business_time as a formatted string for display."""
        result = ""
        if self.business_time.days:
            result = '{} day{}'.format(self.business_time.days, 's' if self.business_time.days > 1 else '')

        hours = int(self.business_time.seconds / 60 / 60)
        if hours:
            result = '{}, {} hour{}'.format(result, int(self.business_time.seconds / 60 / 60), 's' if hours > 1 else '')
        return result

    @property
    def business_time_seconds(self):
        """Returns self.business_time as seconds (computing 8 hours per day.)"""
        return ((self.business_time.days * 8 * 60 * 60) + 
                (self.business_time.seconds))

    @property
    def is_approaching_sla(self):
        """Returns True if this Alert is approaching SLA and has not been dispositioned yet."""
        if hasattr(self, '_is_approaching_sla'):
            return getattr(self, '_is_approaching_sla')

        if self.insert_date is None:
            return None

        if self.sla is None:
            logging.warning("cannot get SLA for {}".format(self))
            return None

        result = False
        if self.disposition is None and self.sla.enabled and self.alert_type not in saq.EXCLUDED_SLA_ALERT_TYPES:
            result = self.business_time_seconds >= (self.sla.timeout - self.sla.warning) * 60 * 60

        setattr(self, '_is_approaching_sla', result)
        return result

    @property
    def is_over_sla(self):
        """Returns True if this Alert is over SLA and has not been dispositioned yet."""
        if hasattr(self, '_is_over_sla'):
            return getattr(self, '_is_over_sla')

        if self.insert_date is None:
            return None

        if self.sla is None:
            logging.warning("cannot get SLA for {}".format(self))
            return None

        result = False
        if self.disposition is None and self.sla.enabled and self.alert_type not in saq.EXCLUDED_SLA_ALERT_TYPES:
            result = self.business_time_seconds >= self.sla.timeout * 60 * 60

        setattr(self, '_is_over_sla', result)
        return result

    tool = Column(
        String(256),
        nullable=False)

    tool_instance = Column(
        String(1024),
        nullable=False)

    alert_type = Column(
        String(64),
        nullable=False)

    description = Column(
        String(1024),
        nullable=False)

    priority = Column(
        Integer,
        nullable=False,
        default=0)

    disposition = Column(
        Enum(
            'FALSE_POSITIVE',
            'IGNORE',
            'UNKNOWN',
            'REVIEWED',
            'GRAYWARE',
            'POLICY_VIOLATION',
            'RECONNAISSANCE',
            'WEAPONIZATION',
            'DELIVERY',
            'EXPLOITATION',
            'INSTALLATION',
            'COMMAND_AND_CONTROL',
            'EXFIL',
            'DAMAGE'),
        nullable=True)

    disposition_user_id = Column(
        Integer,
        ForeignKey('users.id'),
        nullable=True)

    disposition_time = Column(
        TIMESTAMP, 
        nullable=True)

    owner_id = Column(
        Integer,
        ForeignKey('users.id'),
        nullable=True)

    owner_time = Column(
        TIMESTAMP,
        nullable=True)

    archived = Column(
        BOOLEAN, 
        nullable=False,
        default=False)

    removal_user_id = Column(
        Integer,
        ForeignKey('users.id'),
        nullable=True)

    removal_time = Column(
        TIMESTAMP,
        nullable=True)

    lock_owner = Column(
        String(256), 
        nullable=True)

    lock_id = Column(
        String(36),
        nullable=True)

    lock_transaction_id = Column(
        String(36),
        nullable=True)

    lock_time = Column(
        TIMESTAMP, 
        nullable=True)

    detection_count = Column(
        Integer,
        default=0)

    @property
    def status(self):
        status = ''

        if self.workload_item is None:
            if self.lock_id:
                status = 'Analyzing'
                if self.lock_time and lock_expired(self.lock_time):
                    status += ' (expired)'
            else:
                if self.delayed:
                    status = 'Delayed'
                else:
                    status = 'Completed'

        elif self.workload_item.node is None:
            status = 'New'

        else:
            status = 'Assigned'

        if self.removal_time is not None:
            status = '{} (Removed)'.format(status)

        return status

    # relationships
    disposition_user = relationship('saq.database.User', foreign_keys=[disposition_user_id])
    owner = relationship('saq.database.User', foreign_keys=[owner_id])
    remover = relationship('saq.database.User', foreign_keys=[removal_user_id])
    #observable_mapping = relationship('saq.database.ObservableMapping')
    tag_mappings = relationship('saq.database.TagMapping', passive_deletes=True, passive_updates=True)
    #delayed_analysis = relationship('saq.database.DelayedAnalysis')

    @property
    def sorted_tags(self):
        tags = {}
        for tag_mapping in self.tag_mappings:
            tags[tag_mapping.tag.name] = tag_mapping.tag
        return sorted([x for x in tags.values()], key=lambda x: (-x.score, x.name.lower()))

    # we also save these database properties to the JSON data

    KEY_DATABASE_ID = 'database_id'
    KEY_PRIORITY = 'priority'
    KEY_DISPOSITION = 'disposition'
    KEY_DISPOSITION_USER_ID = 'disposition_user_id'
    KEY_DISPOSITION_TIME = 'disposition_time'
    KEY_OWNER_ID = 'owner_id'
    KEY_OWNER_TIME = 'owner_time'
    KEY_REMOVAL_USER_ID = 'removal_user_id'
    KEY_REMOVAL_TIME = 'removal_time'

    @property
    def json(self):
        result = RootAnalysis.json.fget(self)
        result.update({
            Alert.KEY_DATABASE_ID: self.id,
            Alert.KEY_PRIORITY: self.priority,
            Alert.KEY_DISPOSITION: self.disposition,
            Alert.KEY_DISPOSITION_USER_ID: self.disposition_user_id,
            Alert.KEY_DISPOSITION_TIME: self.disposition_time,
            Alert.KEY_OWNER_ID: self.owner_id,
            Alert.KEY_OWNER_TIME: self.owner_time,
            Alert.KEY_REMOVAL_USER_ID: self.removal_user_id,
            Alert.KEY_REMOVAL_TIME: self.removal_time,
        })
        return result

    @json.setter
    def json(self, value):
        assert isinstance(value, dict)
        RootAnalysis.json.fset(self, value)

        if not self.id:
            if Alert.KEY_DATABASE_ID in value:
                self.id = value[Alert.KEY_DATABASE_ID]

        if not self.disposition:
            if Alert.KEY_DISPOSITION in value:
                self.disposition = value[Alert.KEY_DISPOSITION]

        if not self.disposition_user_id:
            if Alert.KEY_DISPOSITION_USER_ID in value:
                self.disposition_user_id = value[Alert.KEY_DISPOSITION_USER_ID]

        if not self.disposition_time:
            if Alert.KEY_DISPOSITION_TIME in value:
                self.disposition_time = value[Alert.KEY_DISPOSITION_TIME]

        if not self.owner_id:
            if Alert.KEY_OWNER_ID in value:
                self.owner_id = value[Alert.KEY_OWNER_ID]

        if not self.owner_time:
            if Alert.KEY_OWNER_TIME in value:
                self.owner_time = value[Alert.KEY_OWNER_TIME]

        if not self.removal_user_id:
            if Alert.KEY_REMOVAL_USER_ID in value:
                self.removal_user_id = value[Alert.KEY_REMOVAL_USER_ID]

        if not self.removal_time:
            if Alert.KEY_REMOVAL_TIME in value:
                self.removal_time = value[Alert.KEY_REMOVAL_TIME]

    def track_delayed_analysis_start(self, observable, analysis_module):
        super().track_delayed_analysis_start(observable, analysis_module)
        with get_db_connection() as db:
            c = db.cursor()
            c.execute("""INSERT INTO delayed_analysis ( alert_id, observable_id, analysis_module ) VALUES ( %s, %s, %s )""",
                     (self.id, observable.id, analysis_module.config_section))
            db.commit()

    def track_delayed_analysis_stop(self, observable, analysis_module):
        super().track_delayed_analysis_stop(observable, analysis_module)
        with get_db_connection() as db:
            c = db.cursor()
            c.execute("""DELETE FROM delayed_analysis where alert_id = %s AND observable_id = %s AND analysis_module = %s""",
                     (self.id, observable.id, analysis_module.config_section))
            db.commit()

    def _handle_tag_added(self, source, event_type, *args, **kwargs):
        assert args
        assert isinstance(args[0], saq.analysis.Tag)
        tag = args[0]

        try:
            self.sync_tag_mapping(tag)
        except Exception as e:
            logging.error("sync_tag_mapping failed: {}".format(e))
            report_exception()

    def sync_profile_points(self):
        logging.debug("syncing profile points for {}".format(self))
        with get_db_connection() as db:
            cursor = db.cursor()
            # make sure all of our profile points have database IDs stored
            # also make a list of all the profile_points ids we have
            current_profile_point_ids = set()
            for profile_point in self.profile_points:
                if profile_point.id is None:
                    cursor.execute("""SELECT id FROM profile_points WHERE description = %s""", (profile_point.description,))
                    row = cursor.fetchone()
                    if row is None:
                        logging.error("unknown profile point {}".format(profile_point))
                    else:
                        profile_point.id = row[0]
                        current_profile_point_ids.add(profile_point.id)
                        logging.debug("found profile point id {} for {}".format(profile_point.id, profile_point))
                else:
                    current_profile_point_ids.add(profile_point.id)

            # check the existing profile point mapping in the database
            # delete any mappings that no longer exist
            deleted_profile_point_ids = set()
            cursor.execute("""
SELECT pp.description FROM profile_points pp 
    JOIN pp_alert_mapping ppam ON ppam.profile_point_id = pp.id 
    JOIN alerts a ON ppam.alert_id = a.id
WHERE
    a.id = %s""", (self.id,))
            for row in cursor:
                profile_point_id = row[0]
                if profile_point_id not in current_profile_point_ids:
                    deleted_profile_point_ids.add(profile_point_id)

            for profile_point in self.profile_points:
                if profile_point.id is None:
                    logging.warning("profile point {} does not have a database ID (skipping sync)".format(
                                    profile_point))
                    continue

                # have we mapped this alert to this profile point yet?
                cursor.execute("""SELECT 1 FROM pp_alert_mapping WHERE alert_id = %s AND profile_point_id = %s""",
                         (self.id, profile_point.id))
                row = cursor.fetchone()
                if not row:
                    cursor.execute("""INSERT INTO pp_alert_mapping ( alert_id, profile_point_id ) VALUES ( %s, %s )""",
                             (self.id, profile_point.id))
                    logging.debug("mapping profile point {} to alert {}".format(profile_point, self))
                else:
                    logging.debug("profile point {} already mapped to {}".format(profile_point, self))

            db.commit()

            for profile_point_id in deleted_profile_point_ids:
                cursor.execute("""DELETE FROM pp_alert_mapping WHERE alert_id = %s AND profile_point_id = %s""",
                         (self.id, profile_point_id))
                logging.debug("deleted profile point mapping {} for {}".format(profile_point_id, self.id))

            db.commit()

    def sync_tag_mapping(self, tag):
        tag_id = None

        with get_db_connection() as db:
            cursor = db.cursor()
            for _ in range(3): # make sure we don't enter an infinite loop here
                cursor.execute("SELECT id FROM tags WHERE name = %s", ( tag.name, ))
                result = cursor.fetchone()
                if result:
                    tag_id = result[0]
                    break
                else:
                    try:
                        execute_with_retry(cursor, "INSERT IGNORE INTO tags ( name ) VALUES ( %s )""", ( tag.name, ))
                        db.commit()
                        continue
                    except pymysql.err.InternalError as e:
                        if e.args[0] == 1062:

                            # another process added it just before we did
                            try:
                                db.rollback()
                            except:
                                pass

                            break
                        else:
                            raise e

            if not tag_id:
                logging.error("unable to find tag_id for tag {}".format(tag.name))
                return

            try:
                execute_with_retry(cursor, "INSERT IGNORE INTO tag_mapping ( alert_id, tag_id ) VALUES ( %s, %s )", ( self.id, tag_id ))
                db.commit()
                logging.debug("mapped tag {} to {}".format(tag, self))
            except pymysql.err.InternalError as e:
                if e.args[0] == 1062: # already mapped
                    return
                else:
                    raise e

    def _handle_observable_added(self, source, event_type, *args, **kwargs):
        assert args
        assert isinstance(args[0], saq.analysis.Observable)
        observable = args[0]

        try:
            self.sync_observable_mapping(observable)
        except Exception as e:
            logging.error("sync_observable_mapping failed: {}".format(e))
            #report_exception()

    def sync_observable_mapping(self, observable):
        observable_id = None

        with get_db_connection() as db:
            cursor = db.cursor()
            for _ in range(3): # make sure we don't enter an infinite loop here
                cursor.execute("SELECT id FROM observables WHERE type = %s AND value = %s", ( observable.type, observable.value ))
                result = cursor.fetchone()
                if result:
                    observable_id = result[0]
                    break
                else:
                    try:
                        execute_with_retry(cursor, "INSERT IGNORE INTO observables ( type, value ) VALUES ( %s, %s )""", 
                                          ( observable.type, observable.value ))
                        db.commit()
                        continue
                    except pymysql.err.InternalError as e:
                        if e.args[0] == 1062:

                            # another process added it just before we did
                            try:
                                db.rollback()
                            except:
                                pass

                            logging.warning("already tracking {}".format(observable))
                            # another process added it just before we did
                            break
                        else:
                            raise e

            if not observable_id:
                logging.error("unable to find observable_id for {}".format(observable))
                return

            try:
                execute_with_retry(cursor, "INSERT IGNORE INTO observable_mapping ( alert_id, observable_id ) VALUES ( %s, %s )", ( self.id, observable_id ))
                db.commit()
                logging.debug("mapped observable {} to {}".format(observable, self))
            except pymysql.err.InternalError as e:
                if e.args[0] == 1062: # already mapped
                    return
                else:
                    raise e

    def sync(self):
        """Saves the Alert to disk and database."""
        assert self.storage_dir is not None # requires a valid storage_dir at this point
        assert isinstance(self.storage_dir, str)

        # newly generated alerts will have a company_name but no company_id
        # we look that up here if we don't have it yet
        if self.company_name and not self.company_id:
            with get_db_connection() as db:
                c = db.cursor()
                c.execute("SELECT `id` FROM company WHERE `name` = %s", (self.company_name))
                row = c.fetchone()
                if row:
                    logging.debug("found company_id {} for company_name {}".format(self.company_id, self.company_name))
                    self.company_id = row[0]

        # compute number of detection points
        self.detection_count = len(self.all_detection_points)

        self.insert()
        if self.id is None:
            logging.error("unable to get the unique id of the alert")
            return False

        self.build_index()

        try:
            self.sync_profile_points()
        except Exception as e:
            logging.error("unable to sync profile points: {}".format(e))
            report_exception()

        self.save() # save this alert now that it has the id

        # we want to unlock it here since the corelation is going to want to pick it up as soon as it gets added
        if self.is_locked():
            self.unlock()

        return True

    def insert(self):
        """Insert this Alert into the alerts database table. Sets the id property of this Alert and returns the id value."""
        new_session = False
        try:
            # do we already have a session we're using?
            session = Session.object_session(self)
            if session is None:
                session = DatabaseSession()
                new_session = True

            self.priority = self.calculate_priority()
            session.add(self)
            session.commit()

            #self.database_id = self.id
            return self.id

        finally:
            # if we opened a new Sesion then we need to make sure we close it when we're done
            if new_session:
                session.close()

    def request_correlation(self):
        """Inserts this alert into the workload of the automated analysis engine."""
        new_session = False
        session = Session.object_session(self)
        if session is None:
            session = DatabaseSession()
            new_session = True

        try:
            logging.info("requesting correlation of {}".format(self))
            session.add(EngineWorkload(alert_id=self.id))
            session.commit()
        finally:
            if new_session:
                session.close()

    #@track_execution_time
    #def sync_tracked_objects(self):
        #"""Updates the observable_mapping and tag_mapping tables according to what objects were added during analysis."""
        # make sure we have something to do
        #if not self._tracked_tags and not self._tracked_observables:
            #return

        #with get_db_connection() as db:
            #c = db.cursor()
            #if self._tracked_tags:
                #logging.debug("syncing {} tags to {}".format(len(self._tracked_tags), self))
                #self._sync_tags(db, c, self._tracked_tags)

            #if self._tracked_observables:
                #logging.debug("syncing {} observables to {}".format(len(self._tracked_observables), self))
                #self._sync_observables(db, c, self._tracked_observables)

            #db.commit()

        #self._tracked_tags.clear()
        #self._tracked_observables.clear()

    #def flush(self):
        #super().flush()
        
        # if this Alert is in the database then
        # we want to go ahead and update if we added any new Tags or Observables
        #if self.id:
            #self.sync_tracked_objects()

    def reset(self):
        super().reset()

        if self.id:
            # rebuild the index after we reset the Alert
            self.rebuild_index()

    @track_execution_time
    def build_index(self):
        """Indexes all Observables and Tags for this Alert."""
        for tag in self.all_tags:
            self.sync_tag_mapping(tag)

        for observable in self.all_observables:
            self.sync_observable_mapping(observable)
        
    @track_execution_time
    def rebuild_index(self):
        """Rebuilds the data for this Alert in the observables, tags, observable_mapping and tag_mapping tables."""
        logging.debug("updating detailed information for {}".format(self))

        with get_db_connection() as db:
            c = db.cursor()
            c.execute("""DELETE FROM observable_mapping WHERE alert_id = %s""", ( self.id, ))
            c.execute("""DELETE FROM tag_mapping WHERE alert_id = %s""", ( self.id, ))
            db.commit()

        self.build_index()

    def similar_alerts(self):
        """Returns list of similar alerts uuid, similarity score and disposition."""
        similarities = []

        #with get_db_connection() as db:
            #c = db.cursor()
            #c.execute("""SELECT count(*) FROM tag_mapping where alert_id = %s group by alert_id""", (self.id))
            #result = c.fetchone()
            #db.commit()
            #if result is None:
                #return similarities

            #num_tags = result[0]
            #if num_tags == 0:
                #return similarities

            #c.execute("""
                #SELECT alerts.uuid, alerts.disposition, 200 * count(*)/(total + %s) AS sim
                #FROM tag_mapping tm1
                #JOIN tag_mapping tm2 ON tm1.tag_id = tm2.tag_id
                #JOIN (SELECT alert_id, count(*) AS total FROM tag_mapping GROUP BY alert_id) AS t1 ON tm1.alert_id = t1.alert_id
                #JOIN alerts on tm1.alert_id = alerts.id
                #WHERE tm2.alert_id = %s AND tm1.alert_id != %s AND alerts.disposition IS NOT NULL AND (alerts.alert_type != 'faqueue' OR (alerts.disposition != 'FALSE_POSITIVE' AND alerts.disposition != 'IGNORE'))
                #GROUP BY tm1.alert_id
                #ORDER BY sim DESC, alerts.disposition_time DESC
                #LIMIT 10
                #""", (num_tags, self.id, self.id))
            #results = c.fetchall()
            #if results is None:
                #return similarities

            #for result in results:
                #similarities.append(Similarity(result[0], result[1], result[2]))

        return similarities

    @property
    def delayed(self):
        try:
            return len(self.delayed_analysis) > 0
        except DetachedInstanceError:
            with get_db_connection() as db:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM delayed_analysis WHERE alert_id = %s", (self.id,))
                result = c.fetchone()
                if not result:
                    return

                return result[0]

    @delayed.setter
    def delayed(self, value):
        pass

class Similarity:
    def __init__(self, uuid, disposition, percent):
        self.uuid = uuid
        self.disposition = disposition
        self.percent = round(float(percent))

class EngineWorkload(Base):

    __tablename__ = 'workload'

    id = Column(
        Integer,
        primary_key=True,
        nullable=False)

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        nullable=False)

    node = Column(String(256), nullable=True, index=True)

    # one-to-one
    alert = relationship('saq.database.Alert', backref=backref('workload_item', uselist=False))

class UserAlertMetrics(Base):
    
    __tablename__ = 'user_alert_metrics'

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        primary_key=True)

    user_id = Column(
        Integer,
        ForeignKey('users.id'),
        primary_key=True)

    start_time = Column(
        TIMESTAMP, 
        nullable=False, 
        server_default=text('CURRENT_TIMESTAMP'))

    disposition_time = Column(
        TIMESTAMP, 
        nullable=True)

    alert = relationship('saq.database.Alert', backref='user_alert_metrics')
    user = relationship('User', backref='user_alert_metrics')

class Comment(Base):

    __tablename__ = 'comments'

    comment_id = Column(
        Integer,
        primary_key=True)

    insert_date = Column(
        TIMESTAMP, 
        nullable=False, 
        index=True,
        server_default=text('CURRENT_TIMESTAMP'))

    user_id = Column(
        Integer,
        ForeignKey('users.id'),
        nullable=False)

    uuid = Column(
        String(36), 
        ForeignKey('alerts.uuid'),
        nullable=False)

    comment = Column(Text)

    # many to one
    user = relationship('User', backref='comments')

class Observable(saq.analysis.Observable, Base):

    __tablename__ = 'observables'

    id = Column(
        Integer,
        primary_key=True)

    type = Column(
        String(64),
        nullable=False)

    value = Column(
        String(1024),
        nullable=False)

    tags = relationship('saq.database.ObservableTagMapping', passive_deletes=True, passive_updates=True)

class ObservableMapping(Base):

    __tablename__ = 'observable_mapping'

    observable_id = Column(
        Integer,
        ForeignKey('observables.id'),
        primary_key=True)

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        primary_key=True)

    alert = relationship('saq.database.Alert', backref='observable_mappings')
    observable = relationship('saq.database.Observable', backref='observable_mappings')

class ObservableTagMapping(Base):
    
    __tablename__ = 'observable_tag_mapping'

    observable_id = Column(
        Integer,
        ForeignKey('observables.id'),
        primary_key=True)

    tag_id = Column(
        Integer,
        ForeignKey('tags.id'),
        primary_key=True)

    observable = relationship('saq.database.Observable', backref='observable_tag_mapping')
    tag = relationship('saq.database.Tag', backref='observable_tag_mapping')

class Tag(saq.analysis.Tag, Base):
    
    __tablename__ = 'tags'

    id = Column(
        Integer,
        primary_key=True)

    name = Column(
        String(256),
        nullable=False)

    @property
    def display(self):
        tag_name = self.name.split(':')[0]
        if tag_name in saq.CONFIG['tags'] and saq.CONFIG['tags'][tag_name] == "special":
            return False
        return True

    @property
    def style(self):
        tag_name = self.name.split(':')[0]
        if tag_name in saq.CONFIG['tags']:
            return saq.CONFIG['tag_css_class'][saq.CONFIG['tags'][tag_name]]
        else:
            return 'label-default'

    #def __init__(self, *args, **kwargs):
        #super(saq.database.Tag, self).__init__(*args, **kwargs)

    @reconstructor
    def init_on_load(self, *args, **kwargs):
        super(saq.database.Tag, self).__init__(*args, **kwargs)

class TagMapping(Base):

    __tablename__ = 'tag_mapping'

    tag_id = Column(
        Integer,
        ForeignKey('tags.id'),
        primary_key=True)

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        primary_key=True)

    alert = relationship('saq.database.Alert', backref='tag_mapping')
    tag = relationship('saq.database.Tag', backref='tag_mapping')

class DelayedAnalysis(Base):

    __tablename__ = 'delayed_analysis'

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        primary_key = True)

    observable_id = Column(
        String(36),
        primary_key = True)

    analysis_module = Column(
        String(512),
        primary_key = True)

    alert = relationship('saq.database.Alert', backref='delayed_analysis')

class ProfilePoint(Base):
    
    __tablename__ = 'profile_points'
    
    id = Column(
        Integer,
        primary_key=True)

    crits_id = Column(
        String(24),
        nullable=False)

    description = Column(
        String(4096),
        nullable=False)

class ProfilePointTagMapping(Base):

    __tablename__ = 'pp_tag_mapping'

    profile_point_id = Column(
        Integer,
        ForeignKey('profile_points.id'),
        primary_key=True)

    tag_id = Column(
        Integer,
        ForeignKey('tags.id'),
        primary_key=True)

    profile_point = relationship('saq.database.ProfilePoint', backref='tag_mappings')
    tag = relationship('saq.database.Tag', backref='profile_point_mappings')

class ProfilePointAlertMapping(Base):
    
    __tablename__ = 'pp_alert_mapping'

    profile_point_id = Column(
        Integer,
        ForeignKey('profile_points.id'),
        primary_key=True)

    alert_id = Column(
        Integer,
        ForeignKey('alerts.id'),
        primary_key=True)

    profile_point = relationship('saq.database.ProfilePoint', backref='alert_mappings')
    alerts = relationship('saq.database.Alert', backref='profile_point_mappings')

class Remediation(Base):

    __tablename__ = 'remediation'

    id = Column(
        Integer,
        primary_key=True)

    type = Column(
        Enum('email'),
        nullable=False,
        default='email')

    action = Column(
        Enum('remove', 'restore'),
        nullable=False,
        default='remove')

    insert_date = Column(
        TIMESTAMP, 
        nullable=False, 
        index=True,
        server_default=text('CURRENT_TIMESTAMP'))

    user_id = Column(
        Integer,
        ForeignKey('users.id'),
        nullable=False)

    key = Column(
        String,
        nullable=False)

    result = Column(
        String,
        nullable=True)

    comment = Column(
        String,
        nullable=True)

    successful = Column(
        BOOLEAN,
        nullable=True,
        default=False)
    
def initialize_database():

    global DatabaseSession
    from config import config

    engine = create_engine(
        config[saq.CONFIG['global']['instance_type']].SQLALCHEMY_DATABASE_URI, 
        **config[saq.CONFIG['global']['instance_type']].SQLALCHEMY_DATABASE_OPTIONS)

    DatabaseSession = sessionmaker(bind=engine)
