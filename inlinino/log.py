import os
from time import gmtime, strftime, time
from struct import pack
import logging
import atexit
import numpy as np


class Log:
    FILE_EXT = 'csv'
    FILE_MODE = 'w'

    def __init__(self, cfg, signal_new_file=None):
        self.__logger = logging.getLogger(self.__class__.__name__)
        # Load Config
        if 'filename_prefix' not in cfg.keys():
            cfg['filename_prefix'] = 'Inlinino'
        if 'path' not in cfg.keys():
            cfg['path'] = ''
        if 'length' not in cfg.keys():
            cfg['length'] = 60  # minutes
        if 'variable_names' not in cfg.keys():
            cfg['variable_names'] = []
        if 'variable_units' not in cfg.keys():
            cfg['variable_units'] = []
        if 'variable_precision' not in cfg.keys():
            cfg['variable_precision'] = []

        self._file = type('obj', (object,), {'closed': True})
        self._file_timestamp = None
        # self.file_mode_binary = cfg['mode_binary']
        self.file_length = cfg['length'] * 60  # seconds
        self.filename_prefix = cfg['filename_prefix']
        self.filename = None
        self.set_filename()
        self.path = cfg['path']
        self.signal_new_file = signal_new_file

        self.variable_names = cfg['variable_names']
        self.variable_units = cfg['variable_units']
        self.variable_precision = cfg['variable_precision']

        atexit.register(self.close)

    def update_cfg(self, cfg):
        self.__logger.debug('Update configuration')
        for k in cfg.keys():
            setattr(self, k, cfg[k])
        self.set_filename()

    def set_filename(self, timestamp=None):
        if timestamp:
            if not os.path.exists(self.path):
                os.makedirs(self.path)
            self.filename = self.filename_prefix + '_' + strftime('%Y%m%d_%H%M%S', gmtime(timestamp)) + \
                            '.' + self.FILE_EXT
            suffix = 0
            while os.path.exists(os.path.join(self.path, self.filename)):
                self.filename = self.filename_prefix + '_' + strftime('%Y%m%d_%H%M%S', gmtime(timestamp)) + \
                                '_' + str(suffix) + '.' + self.FILE_EXT
                suffix += 1
        else:
            self.filename = self.filename_prefix + '_<date>_<time>' + '.' + self.FILE_EXT

    def write_header(self):
        if self.variable_names:
            self._file.write('time, ' + ', '.join(x for x in self.variable_names) + '\n')
            self._file.write('yyyy/mm/dd HH:MM:SS.fff, ' + ', '.join(x for x in self.variable_units) + '\n')

    def open(self, timestamp):
        self.set_filename(timestamp)
        # Create File
        # TODO add exception in case can't open file
        # TODO specify number of bytes in buffer depending on instrument
        self._file = open(os.path.join(self.path, self.filename), self.FILE_MODE)
        self.__logger.info('Open file %s' % self.filename)
        # Write header
        self.write_header()
        # Time file open
        self._file_timestamp = timestamp
        if self.signal_new_file:
            self.signal_new_file.emit()

    def _smart_open(self, timestamp):
        # Open file if necessary
        if self._file.closed or \
                gmtime(self._file_timestamp).tm_mday != gmtime(timestamp).tm_mday or \
                timestamp - self._file_timestamp >= self.file_length:
            # Close previous file if open
            if not self._file.closed:
                self.close()
            # Create new file
            self.open(timestamp)

    def write(self, data, timestamp):
        """
        Write data to file
        :param data: list of values
        :param timestamp: date and time associated with the data frame
        :return:
        """
        self._smart_open(timestamp)
        if self.variable_precision:
            self._file.write(strftime('%Y/%m/%d %H:%M:%S', gmtime(timestamp)) + ("%.3f" % timestamp)[-4:] +
                             ', ' + ', '.join(p % d for p, d in zip(self.variable_precision, data)) + '\n')
        else:
            self._file.write(strftime('%Y/%m/%d %H:%M:%S', gmtime(timestamp)) + ("%.3f" % timestamp)[-4:] +
                             ', ' + ', '.join(str(d) for d in data) + '\n')    
  
    def close(self):
        if not self._file.closed:
            self._file.close()
            self.__logger.debug('Close file %s' % self.filename)
            self.set_filename()
            if self.signal_new_file:
                self.signal_new_file.emit()
        self._file_timestamp = None


class LogBinary(Log):
    FILE_EXT = 'bin'
    FILE_MODE = 'wb'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def write_header(self):
        pass

    def write(self, data, timestamp=None):
        if timestamp:
            self._smart_open(timestamp)
            self._file.write(data + pack('!d', timestamp))
        else:
            # Open file only if doesn't exist (keep in same file as previous bytes logged)
            if self._file.closed:
                self.open(time())
            self._file.write(data)
        # TODO Test unpacking (especially for ACS and HyperSAS)


class LogText(Log):
    FILE_EXT = 'raw'
    ENCODING = 'utf-8'
    UNICODE_HANDLING = 'replace'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration = ''

    def write_header(self):
        self._file.write('time, packet' + '\n')
        self._file.write('yyyy/mm/dd HH:MM:SS.fff, ' + self.ENCODING + '\n')

    def write(self, data, timestamp):
        """
        Write raw ascii data to file
        :param data: typically a binary array of ascii characters
        :param timestamp: date and time associated with the data frame
        :return:
        """
        self._smart_open(timestamp)
        self._file.write(strftime('%Y/%m/%d %H:%M:%S', gmtime(timestamp)) + ("%.3f" % timestamp)[-4:] +
                         ', ' + self.registration + data.decode(self.ENCODING, self.UNICODE_HANDLING) + '\n')
