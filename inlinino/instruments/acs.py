from inlinino.instruments import Instrument
from inlinino.log import LogBinary
from pyACS.acs import ACS as ACSParser
from pyACS.acs import ACSError
import pyqtgraph as pg
from time import time
import numpy as np
from threading import Lock


class ACS(Instrument):

    REGISTRATION_BYTES = b'\xff\x00\xff\x00'
    REQUIRED_CFG_FIELDS = ['device_file',
                           'model', 'serial_number', 'module',
                           'log_path', 'log_raw', 'log_products',
                           'variable_names', 'variable_units', 'variable_precision']

    def __init__(self, cfg_id, signal, *args, **kwargs):
        # ACS Specific attributes
        self._parser = None
        self._timestamp_flag_out_T_cal = 0

        # Init Graphic for real time spectrum visualization
        # TODO Refactor code and move it to GUI
        # Set Color mode
        pg.setConfigOption('background', '#F8F8F2')
        pg.setConfigOption('foreground', '#26292C')
        self._pw = pg.plot(enableMenu=False)
        self._pw.setWindowTitle('ACS Spectrum')
        self._plot = self._pw.plotItem
        self._plot.addLegend()
        # Init Curve Items
        self._plot_curve_c = pg.PlotCurveItem(pen=pg.mkPen(color='#1f77b4', width=2), name='c')
        self._plot_curve_a = pg.PlotCurveItem(pen=pg.mkPen(color='#2ca02c', width=2), name='a')
        # Add item to plot
        self._plot.addItem(self._plot_curve_c)
        self._plot.addItem(self._plot_curve_a)
        # Decoration
        self._plot.setLabel('bottom', 'Wavelength' , units='nm')
        self._plot.setLabel('left', 'Signal', units='m<sup>-1</sup>')
        # self.m_plot.setYRange(0, 5)
        self._plot.setMouseEnabled(x=False, y=True)
        self._plot.showGrid(x=True, y=True)
        self._plot.enableAutoRange(x=True, y=True)
        self._plot.getAxis('left').enableAutoSIPrefix(False)

        super().__init__(cfg_id, signal, *args, **kwargs)

        # Default serial communication parameters
        self.default_serial_baudrate = 115200
        self.default_serial_timeout = 1

        # Auxiliary Data Plugin
        self.plugin_aux_data = True
        self.plugin_aux_data_variable_names = ['Internal Temp. (ºC)', 'External Temp. (ºC)', 'Outside Cal Range']

        # Select Channels to Plot Plugin
        self.plugin_active_timeseries_variables = True
        self.plugin_active_timeseries_variables_names = ['c(%s)' % x for x in self._parser.lambda_c] + \
                                                        ['a(%s)' % x for x in self._parser.lambda_a]
        self.plugin_active_timeseries_variables_selected = []
        self.active_timeseries_variables_lock = Lock()
        self.active_timeseries_c_wavelengths = np.zeros(len(self._parser.lambda_c), dtype=bool)
        self.active_timeseries_a_wavelengths = np.zeros(len(self._parser.lambda_a), dtype=bool)
        for wl in [532]:
            channel_name = 'c(%s)' % self._parser.lambda_c[np.argmin(np.abs(self._parser.lambda_c - wl))]
            self.udpate_active_timeseries_variables(channel_name, True)
        for wl in [532, 676]:
            channel_name = 'a(%s)' % self._parser.lambda_a[np.argmin(np.abs(self._parser.lambda_a - wl))]
            self.udpate_active_timeseries_variables(channel_name, True)

    def setup(self, cfg):
        # Set ACS specific attributes
        if 'device_file' not in cfg.keys():
            raise ValueError('Missing field device file')
        self._parser = ACSParser(cfg['device_file'])
        if 'force_parsing' in cfg.keys():
            self.force_parsing = cfg['force_parsing']
        self.default_serial_baudrate = self._parser.baudrate
        # Overload cfg with ACS specific parameters
        cfg['variable_names'] = ['acs_timestamp', 'c', 'a', 'T_int', 'T_ext', 'flag_outside_calibration_range']
        cfg['variable_units'] = ['ms', '1/m', '1/m', 'deg_C', 'deg_C', 'bool']
        cfg['variable_units'][1] = '1/m\tlambda=' + ' '.join('%s' % x for x in self._parser.lambda_c)
        cfg['variable_units'][2] = '1/m\tlambda=' + ' '.join('%s' % x for x in self._parser.lambda_a)
        cfg['variable_precision'] = ['%d', '%s', '%s', '%.2f', '%.2f', '%s']
        cfg['terminator'] = self.REGISTRATION_BYTES
        # Set standard configuration and check cfg input
        super().setup(cfg, LogBinary)
        # Update Plot config
        min_lambda = min(min(self._parser.lambda_c), min(self._parser.lambda_a))
        max_lambda = max(max(self._parser.lambda_c), max(self._parser.lambda_a))
        self._plot.setXRange(min_lambda, max_lambda)
        self._plot.setLimits(minXRange=min_lambda, maxXRange=max_lambda)

    # def open(self, port=None, baudrate=None, bytesize=8, parity='N', stopbits=1, timeout=1):
    #     if baudrate is None:
    #         # Get default baudrate from device file via parser
    #         baudrate = self._parser.baudrate  # Default 115200
    #     super().open(port, baudrate, bytesize, parity, stopbits, timeout)

    def data_received(self, data, timestamp):
        self._buffer.extend(data)
        frame = True
        while frame:
            # Get Frame
            frame, valid, self._buffer, unknown_bytes = self._parser.find_frame(self._buffer)
            if unknown_bytes:
                # Log bytes in raw files (no warning as expect the pad bytes here)
                if self.log_raw_enabled and self._log_active:
                    self._log_raw.write(unknown_bytes)
            if frame and valid:
                self.handle_packet(frame, timestamp)
            if frame and not valid:
                # Warn user
                # Log only registration bytes as rest will be logged by unknown_bytes
                self.signal.packet_corrupted.emit()
                if self.log_raw_enabled and self._log_active:
                    self._log_raw.write(self._parser.REGISTRATION_BYTES)

    def parse(self, packet):
        data_raw = self._parser.unpack_frame(packet)
        try:
            self._parser.check_data(data_raw)
        except ACSError as e:
            self.signal.packet_corrupted.emit()
            self.logger.warning(e)
            self.logger.debug(self.REGISTRATION_BYTES + packet)
        data_cal = self._parser.calibrate_frame(data_raw, get_external_temperature=True)
        return data_raw.time_stamp, data_cal

    def handle_data(self, data, timestamp):
        # Update timeseries plot
        if self.active_timeseries_variables_lock.acquire(timeout=0.125):
            try:
                self.signal.new_data.emit(np.concatenate((data[1].c[self.active_timeseries_c_wavelengths],
                                                          data[1].a[self.active_timeseries_a_wavelengths])),
                                          timestamp)
            finally:
                self.active_timeseries_variables_lock.release()
        else:
            self.logger.error('Unable to acquire lock to update timeseries plot')
        # Format and signal aux data
        self.signal.new_aux_data.emit(['%.2f' % data[1].internal_temperature,
                                       '%.2f' % data[1].external_temperature,
                                       '%s' % data[1].flag_outside_calibration_range])
        # Update spectrum plot
        sel = np.logical_not(np.logical_or(np.isinf(data[1].c), np.isnan(data[1].c)))
        self._plot_curve_c.setData(self._parser.lambda_c[sel], data[1].c[sel])
        sel = np.logical_not(np.logical_or(np.isinf(data[1].a), np.isnan(data[1].a)))
        self._plot_curve_a.setData(self._parser.lambda_a[sel], data[1].a[sel])
        # Flag outside temperature calibration range
        if data[1].flag_outside_calibration_range and time() - self._timestamp_flag_out_T_cal > 120:
            self._timestamp_flag_out_T_cal = time()
            self.logger.warning('Internal temperature outside calibration range.')
        # Log parsed data
        if self.log_prod_enabled and self._log_active:
            self._log_prod.write([data[0],  # Instrument timestamp
                                  np.array2string(data[1].c, max_line_width=np.inf),  # pre-format np.array
                                  np.array2string(data[1].a, max_line_width=np.inf),  # pre-format np.array
                                  data[1].internal_temperature, data[1].external_temperature,
                                  data[1].flag_outside_calibration_range], timestamp)
            if not self.log_raw_enabled:
                self.signal.packet_logged.emit()

    def udpate_active_timeseries_variables(self, name, state):
        if not ((state and name not in self.plugin_active_timeseries_variables_selected) or
                (not state and name in self.plugin_active_timeseries_variables_selected)):
            return
        if self.active_timeseries_variables_lock.acquire(timeout=0.25):
            try:
                if name[0] == 'c':
                    index = self.plugin_active_timeseries_variables_names.index(name)
                    self.active_timeseries_c_wavelengths[index] = state
                elif name[0] == 'a':
                    offset = len(self._parser.lambda_c)
                    index = self.plugin_active_timeseries_variables_names.index(name, offset) - offset
                    self.active_timeseries_a_wavelengths[index] = state
            finally:
                self.active_timeseries_variables_lock.release()
        else:
            self.logger.error('Unable to acquire lock to update active timeseries variables')
        # Update list of active variables for GUI keeping the order
        self.plugin_active_timeseries_variables_selected = \
            ['c(%s)' % wl for wl in self._parser.lambda_c[self.active_timeseries_c_wavelengths]] + \
            ['a(%s)' % wl for wl in self._parser.lambda_a[self.active_timeseries_a_wavelengths]]
