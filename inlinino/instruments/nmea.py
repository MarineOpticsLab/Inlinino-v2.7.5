from inlinino.instruments import Instrument
import numpy as np
import pynmea2


class NMEA(Instrument):

    REQUIRED_CFG_FIELDS = ['model', 'serial_number', 'module',
                           'log_path', 'log_raw', 'log_products',
                           'variable_names', 'variable_units', 'variable_types', 'variable_precision']

    def __init__(self, cfg_id, signal, *args, **kwargs):
        self.active_timeseries_variables = []
        self.plugin_active_timeseries_variables_selected = list()
        self._unknown_nmea_sentence = []
        super().__init__(cfg_id, signal, *args, **kwargs)

        # Default serial communication parameters
        self.default_serial_baudrate = 4800
        self.default_serial_timeout = 10

    def setup(self, cfg):
        # Overload cfg
        cfg['terminator'] = b'\r\n'
        # Set standard configuration and check cfg input
        super().setup(cfg)
        # Set active timeseries variables
        self.active_timeseries_variables = np.zeros(len(self.variable_types), dtype=bool)
        self.plugin_active_timeseries_variables_selected = list()
        for i, (k, t) in enumerate(zip(self.variable_names, self.variable_types)):
            if t in ['int', 'float']:
                self.active_timeseries_variables[i] = True
                self.plugin_active_timeseries_variables_selected.append(k)
        # self._log_prod.variable_precision = []  # Disable precision when writing with log

    # def open(self, port=None, baudrate=4800, bytesize=8, parity='N', stopbits=1, timeout=10):
    #     super().open(port, baudrate, bytesize, parity, stopbits, timeout)

    def parse(self, packet):
        msg = pynmea2.parse(packet.decode())
        # except ValueError:
        #     msg = packet.decode()
        #     if msg[0] == '$':
        #         header = msg.split(',', 1)[0]
        #         if header not in self._unknown_nmea_sentence:
        #             self._unknown_nmea_sentence.append(header)
        #             self.signal.packet_corrupted.emit()
        #             self.logger.warning(f'Unknown NMEA sentence: {header}')
        # print(packet)
        # print(msg.fields)
        data = [None] * len(self.variable_names)
        for i, (k, t) in enumerate(zip(self.variable_names, self.variable_types)):
            try:
                if t == 'int':
                    data[i] = int(getattr(msg, k)) if hasattr(msg, k) and getattr(msg, k) != '' else float('nan')
                elif t == 'float':
                    data[i] = float(getattr(msg, k)) if hasattr(msg, k) and getattr(msg, k) != '' else float('nan')
                elif t == 'str':
                    data[i] = str(getattr(msg, k)) if hasattr(msg, k) else 'nan'
                else:
                    raise ValueError("Variable type not supported.")
            except TypeError:
                # Typical of pynmea2 unable to parse datetime
                data[i] = 'nan' if t == 'str' else float('nan')
        return data

    def handle_data(self, data, timestamp):
        if np.any(self.active_timeseries_variables):
            self.signal.new_data.emit(np.array(data)[self.active_timeseries_variables], timestamp)
        if self.log_prod_enabled and self._log_active:
            self._log_prod.write(data, timestamp)
            if not self.log_raw_enabled:
                self.signal.packet_logged.emit()