import serial
from pyqtgraph.Qt import QtGui, QtCore, QtWidgets, uic
from PyQt5 import QtMultimedia
import pyqtgraph as pg
import sys, os, glob
import logging
from time import time, gmtime, strftime
from serial.tools.list_ports import comports as list_serial_comports
from inlinino import RingBuffer, CFG, __version__, PATH_TO_RESOURCES
from inlinino.instruments import Instrument, SerialInterface, SocketInterface, InterfaceException
from inlinino.instruments.acs import ACS
from inlinino.instruments.dataq import DATAQ
from inlinino.instruments.hyperbb import HyperBB
from inlinino.instruments.lisst import LISST
from inlinino.instruments.nmea import NMEA
from inlinino.instruments.taratsg import TaraTSG
from pyACS.acs import ACS as ACSParser
from inlinino.instruments.lisst import LISSTParser
import numpy as np
from math import floor

logger = logging.getLogger('GUI')


class InstrumentSignals(QtCore.QObject):
    status_update = QtCore.pyqtSignal()
    packet_received = QtCore.pyqtSignal()
    packet_corrupted = QtCore.pyqtSignal()
    packet_logged = QtCore.pyqtSignal()
    new_data = QtCore.pyqtSignal(object, float)
    new_aux_data = QtCore.pyqtSignal(list)
    alarm = QtCore.pyqtSignal(bool)


def seconds_to_strmmss(seconds):
    min = floor(seconds / 60)
    sec = seconds % 60
    return '%d:%02d' % (min, sec)


class MainWindow(QtGui.QMainWindow):
    BACKGROUND_COLOR = '#F8F8F2'
    FOREGROUND_COLOR = '#26292C'
    PEN_COLORS = ['#1f77b4',  # muted blue
                  '#2ca02c',  # cooked asparagus green
                  '#ff7f0e',  # safety orange
                  '#d62728',  # brick red
                  '#9467bd',  # muted purple
                  '#8c564b',  # chestnut brown
                  '#e377c2',  # raspberry yogurt pink
                  '#7f7f7f',  # middle gray
                  '#bcbd22',  # curry yellow-green
                  '#17becf']  # blue-teal
    BUFFER_LENGTH = 240
    MAX_PLOT_REFRESH_RATE = 4   # Hz

    def __init__(self, instrument=None):
        super(MainWindow, self).__init__()
        uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'main.ui'), self)
        # Graphical Adjustments
        self.dock_widget.setTitleBarWidget(QtGui.QWidget(None))
        self.label_app_version.setText('Inlinino v' + __version__)
        # Set Colors
        palette = QtGui.QPalette()
        palette.setColor(palette.Window, QtGui.QColor(self.BACKGROUND_COLOR))  # Background
        palette.setColor(palette.WindowText, QtGui.QColor(self.FOREGROUND_COLOR))  # Foreground
        self.setPalette(palette)
        pg.setConfigOption('background', pg.mkColor(self.BACKGROUND_COLOR))
        pg.setConfigOption('foreground', pg.mkColor(self.FOREGROUND_COLOR))
        # Set figure with pyqtgraph
        # pg.setConfigOption('antialias', True)  # Lines are drawn with smooth edges at the cost of reduced performance
        self._buffer_timestamp = None
        self._buffer_data = []
        self.last_plot_refresh = time()
        self.timeseries_widget = None
        self.init_timeseries_plot()
        # Set instrument
        if instrument:
            self.init_instrument(instrument)
        else:
            self.instrument = None
        self.packets_received = 0
        self.packets_logged = 0
        self.packets_corrupted = 0
        self.packets_corrupted_flag = False
        self.last_packet_corrupted_timestamp = 0
        # Set buttons
        self.button_setup.clicked.connect(self.act_instrument_setup)
        self.button_serial.clicked.connect(self.act_instrument_interface)
        self.button_log.clicked.connect(self.act_instrument_log)
        self.button_figure_clear.clicked.connect(self.act_clear_timeseries_plot)
        # Set clock
        self.signal_clock = QtCore.QTimer()
        self.signal_clock.timeout.connect(self.set_clock)
        self.signal_clock.start(1000)
        # Alarm message box for data timeout
        self.alarm_sound = QtMultimedia.QMediaPlayer()
        self.alarm_playlist = QtMultimedia.QMediaPlaylist(self.alarm_sound)
        for file in sorted(glob.glob(os.path.join(PATH_TO_RESOURCES, 'alarm*.wav'))):
            self.alarm_playlist.addMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(file)))
        if self.alarm_playlist.mediaCount() < 1:
            logger.warning('No alarm sounds available: disabled alarm')
        self.alarm_playlist.setPlaybackMode(QtMultimedia.QMediaPlaylist.Loop)  # Playlist is needed for infinite loop
        self.alarm_sound.setPlaylist(self.alarm_playlist)
        # self.alarm_sound.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(
        #     os.path.join(PATH_TO_RESOURCES, 'alarm-arcade.wav'))))
        self.alarm_message_box_active = False
        self.alarm_message_box = QtWidgets.QMessageBox()
        self.alarm_message_box.setIcon(QtWidgets.QMessageBox.Warning)
        self.alarm_message_box.setWindowTitle("Data Timeout Alarm")
        self.alarm_message_box.setText("An error with the serial connection occured or "
                                       "no data was received in the past minute.\n\n"
                                       "Does the instrument receive power?\n"
                                       "Are the serial cable and serial to USB adapter connected?\n"
                                       "Is the instruments set to continuously send data?\n")
        self.alarm_message_box.setStandardButtons(QtWidgets.QMessageBox.Ignore)
        self.alarm_message_box.buttonClicked.connect(self.alarm_message_box_button_clicked)
        # Plugins variables
        self.plugin_aux_data_variable_names = []
        self.plugin_aux_data_variable_values = []

    def init_instrument(self, instrument):
        self.instrument = instrument
        self.label_instrument_name.setText(self.instrument.name)
        self.instrument.signal.status_update.connect(self.on_status_update)
        self.instrument.signal.packet_received.connect(self.on_packet_received)
        self.instrument.signal.packet_corrupted.connect(self.on_packet_corrupted)
        self.instrument.signal.packet_logged.connect(self.on_packet_logged)
        self.instrument.signal.new_data.connect(self.on_new_data)
        self.instrument.signal.alarm.connect(self.on_data_timeout)
        self.on_status_update()  # Need to be run as on instrument setup the signals were not connected

        # Set Plugins specific to instrument
        # Auxiliary Data Plugin
        self.group_box_aux_data.setVisible(self.instrument.plugin_aux_data)
        if self.instrument.plugin_aux_data:
            # Set aux variable names
            for v in self.instrument.plugin_aux_data_variable_names:
                self.plugin_aux_data_variable_names.append(QtGui.QLabel(v))
                self.plugin_aux_data_variable_values.append(QtGui.QLabel('?'))
                self.group_box_aux_data_layout.addRow(self.plugin_aux_data_variable_names[-1],
                                                      self.plugin_aux_data_variable_values[-1])
            # Connect signal
            self.instrument.signal.new_aux_data.connect(self.on_new_aux_data)

        # Select Channels To Plot Plugin
        self.group_box_active_timeseries_variables.setVisible(self.instrument.plugin_active_timeseries_variables)
        if self.instrument.plugin_active_timeseries_variables:
            # Set sel channels check_box
            for v in self.instrument.plugin_active_timeseries_variables_names:
                check_box = QtWidgets.QCheckBox(v)
                check_box.stateChanged.connect(self.on_active_timeseries_variables_update)
                if v in self.instrument.plugin_active_timeseries_variables_selected:
                    check_box.setChecked(True)
                self.group_box_active_timeseries_variables_scroll_area_content_layout.addWidget(check_box)

    def init_timeseries_plot(self):
        self.timeseries_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem(utcOffset=0)}, enableMenu=False)
        self.timeseries_widget.plotItem.setLabel('bottom', 'Time ', units='UTC')
        self.timeseries_widget.plotItem.getAxis('bottom').enableAutoSIPrefix(False)
        self.timeseries_widget.plotItem.setLabel('left', 'Signal')
        self.timeseries_widget.plotItem.getAxis('left').enableAutoSIPrefix(False)
        self.timeseries_widget.plotItem.setLimits(minYRange=0, maxYRange=4500)  # In version 0.9.9
        self.timeseries_widget.plotItem.setMouseEnabled(x=False, y=True)
        self.timeseries_widget.plotItem.showGrid(x=False, y=True)
        self.timeseries_widget.plotItem.enableAutoRange(x=True, y=True)
        self.timeseries_widget.plotItem.addLegend()
        self.setCentralWidget(self.timeseries_widget)

    def set_clock(self):
        zulu = gmtime(time())
        self.label_clock.setText(strftime('%H:%M:%S', zulu) + ' UTC')
        # self.label_date.setText(strftime('%Y/%m/%d', zulu))

    def act_instrument_setup(self):
        logger.debug('Setup instrument')
        setup_dialog = DialogInstrumentSetup(self.instrument.cfg_id, self)
        setup_dialog.show()
        if setup_dialog.exec_():
            self.instrument.setup(setup_dialog.cfg)
            self.label_instrument_name.setText(self.instrument.name)

    def act_instrument_interface(self):
        if self.instrument.alive:
            logger.debug('Disconnect instrument')
            self.instrument.close()
        else:
            if type(self.instrument._interface) == SerialInterface:
                dialog = DialogSerialConnection(self)
            elif type(self.instrument._interface) == SocketInterface:
                dialog = DialogSocketConnection(self)
            dialog.show()
            if dialog.exec_():
                try:
                    if type(self.instrument._interface) == SerialInterface:
                        self.instrument.open(port=dialog.port, baudrate=dialog.baudrate, bytesize=dialog.bytesize,
                                             parity=dialog.parity, stopbits=dialog.stopbits, timeout=dialog.timeout)
                    elif type(self.instrument._interface) == SocketInterface:
                        self.instrument.open(ip=dialog.ip, port=dialog.port)
                except InterfaceException as e:
                    QtGui.QMessageBox.warning(self, "Inlinino: Connect " + self.instrument.name,
                                              'ERROR: Failed connecting ' + self.instrument.name + '. ' +
                                              str(e),
                                              QtGui.QMessageBox.Ok)

    def act_instrument_log(self):
        if self.instrument.log_active():
            logger.debug('Stop logging')
            self.instrument.log_stop()
        else:
            dialog = DialogLoggerOptions(self)
            dialog.show()
            if dialog.exec_():
                self.instrument.log_update_cfg({'filename_prefix': dialog.cover_log_prefix +
                                                                   self.instrument.bare_log_prefix,
                                                'path': dialog.log_path})
                logger.debug('Start logging')
                self.instrument.log_start()

    def act_clear_timeseries_plot(self):
        # Send no data which reset buffers
        self.instrument.signal.new_data.emit([], time())

    @QtCore.pyqtSlot()
    def on_status_update(self):
        if self.instrument.alive:
            self.button_serial.setText('Close')
            self.button_serial.setToolTip('Disconnect instrument.')
            self.button_log.setEnabled(True)
            if self.instrument.log_active():
                status = 'Logging'
                if self.instrument.log_raw_enabled:
                    if self.instrument.log_prod_enabled:
                        status += ' (raw & prod)'
                    else:
                        status += ' (raw)'
                else:
                    status += ' (prod)'
                self.label_status.setText(status)
                self.label_instrument_name.setStyleSheet('font: 24pt;\ncolor: #12ab29;')
                # Green: #12ab29 (darker) #29ce42 (lighter) #9ce22e (pyQtGraph)
                self.button_log.setText('Stop')
                self.button_log.setToolTip('Stop logging data')
            else:
                self.label_status.setText('Connected')
                self.label_instrument_name.setStyleSheet('font: 24pt;\ncolor: #ff9e17;')
                # Orange: #ff9e17 (darker) #ffc12f (lighter)
                self.button_log.setText('Start')
                self.button_log.setToolTip('Start logging data')
        else:
            self.label_status.setText('Disconnected')
            self.label_instrument_name.setStyleSheet('font: 24pt;\ncolor: #e0463e;')
            # Red: #e0463e (darker) #5cd9ef (lighter)  #f92670 (pyQtGraph)
            self.button_serial.setText('Open')
            self.button_serial.setToolTip('Connect instrument.')
            self.button_log.setEnabled(False)
        self.le_filename.setText(self.instrument.log_get_filename())
        self.le_directory.setText(self.instrument.log_get_path())
        self.packets_received = 0
        self.label_packets_received.setText(str(self.packets_received))
        self.packets_logged = 0
        self.label_packets_logged.setText(str(self.packets_logged))
        self.packets_corrupted = 0
        self.label_packets_corrupted.setText(str(self.packets_corrupted))

    @QtCore.pyqtSlot()
    def on_packet_received(self):
        self.packets_received += 1
        self.label_packets_received.setText(str(self.packets_received))
        if self.packets_corrupted_flag and time() - self.last_packet_corrupted_timestamp > 5:
            self.label_packets_corrupted.setStyleSheet(f'font-weight:normal;color: {self.FOREGROUND_COLOR};')
            self.packets_corrupted_flag = False

    @QtCore.pyqtSlot()
    def on_packet_logged(self):
        self.packets_logged += 1
        if self.packets_received < self.packets_logged < 2:  # Fix inconsistency when start logging
            self.packets_received = self.packets_logged
            self.label_packets_received.setText(str(self.packets_received))
        self.label_packets_logged.setText(str(self.packets_logged))

    @QtCore.pyqtSlot()
    def on_packet_corrupted(self):
        ts = time()
        self.packets_corrupted += 1
        self.label_packets_corrupted.setText(str(self.packets_corrupted))
        if ts - self.last_packet_corrupted_timestamp < 5:  # seconds
            self.label_packets_corrupted.setStyleSheet('font-weight:bold;color: #e0463e;')  # red
            self.packets_corrupted_flag = True
        self.last_packet_corrupted_timestamp = ts

    @QtCore.pyqtSlot(list, float)
    @QtCore.pyqtSlot(np.ndarray, float)
    def on_new_data(self, data, timestamp):
        if len(self._buffer_data) != len(data):
            # Init buffers
            self._buffer_timestamp = RingBuffer(self.BUFFER_LENGTH)
            self._buffer_data = [RingBuffer(self.BUFFER_LENGTH) for i in range(len(data))]
            # Init Plot (need to do so when number of curve changes)
            self.init_timeseries_plot()
            # Init curves
            if hasattr(self.instrument, 'plugin_active_timeseries_variables_selected'):
                legend = self.instrument.plugin_active_timeseries_variables_selected
            else:
                legend = self.instrument.variable_names
            for i in range(len(data)):
                self.timeseries_widget.plotItem.addItem(
                    pg.PlotCurveItem(pen=pg.mkPen(color=self.PEN_COLORS[i % len(self.PEN_COLORS)], width=2),
                                     name=legend[i])
                )
        # Update buffers
        self._buffer_timestamp.extend(timestamp)
        for i in range(len(data)):
            self._buffer_data[i].extend(data[i])
        # TODO Update real-time figure (depend on instrument type)
        # Update timeseries figure
        if time() - self.last_plot_refresh < 1 / self.MAX_PLOT_REFRESH_RATE:
            return
        timestamp = self._buffer_timestamp.get(self.BUFFER_LENGTH)  # Not used anymore
        for i in range(len(data)):
            y = self._buffer_data[i].get(self.BUFFER_LENGTH)
            x = np.arange(len(y))
            y[np.isinf(y)] = 0
            nsel = np.isnan(y)
            if not np.all(nsel):
                sel = np.logical_not(nsel)
                y[nsel] = np.interp(x[nsel], x[sel], y[sel])
                # self.timeseries_widget.plotItem.items[i].setData(y, connect="finite")
                self.timeseries_widget.plotItem.items[i].setData(timestamp[sel], y[sel], connect="finite")
        self.timeseries_widget.plotItem.enableAutoRange(x=True)  # Needed as somehow the user disable sometimes
        self.last_plot_refresh = time()

    @QtCore.pyqtSlot(list)
    def on_new_aux_data(self, data):
        if self.instrument.plugin_aux_data:
            for i, v in enumerate(data):
                self.plugin_aux_data_variable_values[i].setText(str(v))

    @QtCore.pyqtSlot(int)
    def on_active_timeseries_variables_update(self, state):
        if self.instrument.plugin_active_timeseries_variables:
            self.instrument.udpate_active_timeseries_variables(self.sender().text(), state)

    @QtCore.pyqtSlot(bool)
    def on_data_timeout(self, active):
        if active and not self.alarm_message_box_active:
            # Start alarm and Open message box
            self.alarm_playlist.setCurrentIndex(0)
            self.alarm_sound.play()
            self.alarm_message_box.open()
            getattr(self.alarm_message_box, 'raise')
            self.alarm_message_box_active = True
        elif not active and self.alarm_message_box_active:
            # Stop alarm and Close message box
            self.alarm_sound.stop()
            self.alarm_message_box.close()
            self.alarm_message_box_active = False

    def alarm_message_box_button_clicked(self, button):
        if button.text() == 'Ignore':
            logger.info('Ignored alarm')
            self.alarm_sound.stop()
            self.alarm_message_box.close()
            self.alarm_message_box_active = False

    def closeEvent(self, event):
        msg = QtGui.QMessageBox()
        msg.setIcon(QtGui.QMessageBox.Question)
        msg.setWindowTitle("Inlinino: Closing Application")
        msg.setText("Are you sure to quit ?")
        msg.setStandardButtons(QtGui.QMessageBox.Yes | QtGui.QMessageBox.No)
        msg.setDefaultButton(QtGui.QMessageBox.No)
        if msg.exec_() == QtGui.QMessageBox.Yes:
            QtGui.QApplication.instance().closeAllWindows()  # NEEDED IF OTHER WINDOWS OPEN BY SPECIFIC INSTRUMENTS
            event.accept()
        else:
            event.ignore()


class DialogStartUp(QtGui.QDialog):
    LOAD_INSTRUMENT = 1
    SETUP_INSTRUMENT = 2

    def __init__(self):
        super(DialogStartUp, self).__init__()
        uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'startup.ui'), self)
        instruments_to_load = [i["manufacturer"] + ' ' + i["model"] + ' ' + i["serial_number"] for i in CFG.instruments]
        # self.instruments_to_setup = [i[6:-3] for i in sorted(os.listdir(PATH_TO_RESOURCES)) if i[-3:] == '.ui' and i[:6] == 'setup_']
        self.instruments_to_setup = [os.path.basename(i)[6:-3] for i in sorted(glob.glob(os.path.join(PATH_TO_RESOURCES, 'setup_*.ui')))]
        self.combo_box_instrument_to_load.addItems(instruments_to_load)
        self.combo_box_instrument_to_setup.addItems(self.instruments_to_setup)
        self.button_load.clicked.connect(self.act_load_instrument)
        self.button_setup.clicked.connect(self.act_setup_instrument)
        self.selection_index = None

    def act_load_instrument(self):
        self.selection_index = self.combo_box_instrument_to_load.currentIndex()
        self.done(self.LOAD_INSTRUMENT)

    def act_setup_instrument(self):
        self.selection_index = self.combo_box_instrument_to_setup.currentIndex()
        self.done(self.SETUP_INSTRUMENT)


class DialogInstrumentSetup(QtGui.QDialog):
    ENCODING = 'ascii'
    OPTIONAL_FIELDS = ['Variable Precision', 'Prefix Custom']

    def __init__(self, template, parent=None):
        super().__init__(parent)
        if isinstance(template, str):
            # Load template from instrument type
            self.create = True
            self.cfg_index = -1
            self.cfg = {'module': template}
            uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'setup_' + template + '.ui'), self)
        elif isinstance(template, int):
            # Load from preconfigured instrument
            self.create = False
            self.cfg_index = template
            self.cfg = CFG.instruments[template]
            uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'setup_' + self.cfg['module'] + '.ui'), self)
            # Populate fields
            for k, v in self.cfg.items():
                if hasattr(self, 'le_' + k):
                    if isinstance(v, bytes):
                        getattr(self, 'le_' + k).setText(v.decode().encode('unicode_escape').decode())
                    elif isinstance(v, list):
                        getattr(self, 'le_' + k).setText(', '.join([str(vv) for vv in v]))
                    else:
                        getattr(self, 'le_' + k).setText(v)
                elif hasattr(self, 'combobox_' + k):
                    if v:
                        getattr(self, 'combobox_' + k).setCurrentIndex(0)
                    else:
                        getattr(self, 'combobox_' + k).setCurrentIndex(1)
            # Populate special fields specific to each module
            if self.cfg['module'] == 'dataq':
                for c in self.cfg['channels_enabled']:
                    getattr(self, 'checkbox_channel%d_enabled' % (c + 1)).setChecked(True)
            if hasattr(self, 'combobox_interface'):
                if 'interface' in self.cfg.keys():
                    if self.cfg['interface'] == 'serial':
                        self.combobox_interface.setCurrentIndex(0)
                    elif self.cfg['interface'] == 'socket':
                        self.combobox_interface.setCurrentIndex(1)
        else:
            raise ValueError('Invalid instance type for template.')
        if 'button_browse_log_directory' in self.__dict__.keys():
            self.button_browse_log_directory.clicked.connect(self.act_browse_log_directory)
        if 'button_browse_device_file' in self.__dict__.keys():
            self.button_browse_device_file.clicked.connect(self.act_browse_device_file)
        if 'button_browse_ini_file' in self.__dict__.keys():
            self.button_browse_ini_file.clicked.connect(self.act_browse_ini_file)
        if 'button_browse_dcal_file' in self.__dict__.keys():
            self.button_browse_dcal_file.clicked.connect(self.act_browse_dcal_file)
        if 'button_browse_zsc_file' in self.__dict__.keys():
            self.button_browse_zsc_file.clicked.connect(self.act_browse_zsc_file)
        if 'button_browse_plaque_file' in self.__dict__.keys():
            self.button_browse_plaque_file.clicked.connect(self.act_browse_plaque_file)
        if 'button_browse_temperature_file' in self.__dict__.keys():
            self.button_browse_temperature_file.clicked.connect(self.act_browse_temperature_file)

        # Cannot use default save button as does not provide mean to correctly validate user input
        self.button_save = QtGui.QPushButton('Save')
        self.button_save.setDefault(True)
        self.button_save.clicked.connect(self.act_save)
        self.button_box.addButton(self.button_save, QtGui.QDialogButtonBox.ActionRole)
        self.button_box.rejected.connect(self.reject)

    def act_browse_log_directory(self):
        self.le_log_path.setText(QtGui.QFileDialog.getExistingDirectory(caption='Choose logging directory'))

    def act_browse_device_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose device file', filter='Device File (*.dev *.txt)')
        self.le_device_file.setText(file_name)

    def act_browse_ini_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose initialization file', filter='Ini File (*.ini)')
        self.le_ini_file.setText(file_name)

    def act_browse_dcal_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose DCAL file', filter='DCAL File (*.asc)')
        self.le_dcal_file.setText(file_name)

    def act_browse_zsc_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose ZSC file', filter='ZSC File (*.asc)')
        self.le_zsc_file.setText(file_name)

    def act_browse_plaque_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose plaque calibration file', filter='Plaque File (*.mat)')
        self.le_plaque_file.setText(file_name)

    def act_browse_temperature_file(self):
        file_name, selected_filter = QtGui.QFileDialog.getOpenFileName(
            caption='Choose temperature calibration file', filter='Temperature File (*.mat)')
        self.le_temperature_file.setText(file_name)

    def act_save(self):
        # Read form
        fields = [a for a in self.__dict__.keys() if 'combobox_' in a or 'le_' in a]
        empty_fields = list()
        for f in fields:
            field_prefix, field_name = f.split('_', 1)
            field_pretty_name = field_name.replace('_', ' ').title()
            if f == 'combobox_interface':
                self.cfg[field_name] = self.combobox_interface.currentText()
            elif field_prefix == 'le':
                value = getattr(self, f).text()
                if not value:
                    empty_fields.append(field_pretty_name)
                    continue
                # Apply special formatting to specific variables
                try:
                    if 'variable_' in field_name:
                        value = value.split(',')
                        value = [v.strip() for v in value]
                        if 'variable_columns' in field_name:
                            value = [int(x) for x in value]
                    elif field_name in ['terminator', 'separator']:
                        # if len(value) > 3 and (value[:1] == "b'" and value[-1] == "'"):
                        #     value = bytes(value[2:-1], 'ascii')
                        value = value.strip().encode(self.ENCODING).decode('unicode_escape').encode(self.ENCODING)
                    else:
                        value.strip()
                except:
                    self.notification('Unable to parse special variable: ' + field_pretty_name, sys.exc_info()[0])
                    return
                self.cfg[field_name] = value
            elif field_prefix == 'combobox':
                if getattr(self, f).currentText() == 'on':
                    self.cfg[field_name] = True
                else:
                    self.cfg[field_name] = False
        for f in self.OPTIONAL_FIELDS:
            try:
                empty_fields.pop(empty_fields.index(f))
            except ValueError:
                pass
        if empty_fields:
            self.notification('Fill required fields.', '\n'.join(empty_fields))
            return
        # Check fields specific to modules
        if self.cfg['module'] == 'generic':
            variable_keys = [v for v in self.cfg.keys() if 'variable_' in v]
            if variable_keys:
                # Check length
                n = len(self.cfg['variable_names'])
                for k in variable_keys:
                    if n != len(self.cfg[k]):
                        self.notification('Inconsistent length. Variable Names, Variable Units, Variable Columns,'
                                          'Variable Types, and Variable Precision must have the same number of elements '
                                          'separated by commas.')
                        return
                # Check type
                for v in self.cfg['variable_types']:
                    if v not in ['int', 'float']:
                        self.notification('Invalid variable type')
                        return
                # Check precision
                if 'variable_precision' in self.cfg:
                    for v in self.cfg['variable_precision']:
                        if v[0] != '%' and v[-1] not in ['d', 'f']:
                            self.notification('Invalid variable precision. '
                                              'Expect type specific formatting (e.g. %d or %.3f) separated by commas.')
                            return
            if not self.cfg['log_raw'] and not self.cfg['log_products']:
                self.notification('Invalid logger configuration. '
                                  'At least one logger must be ON (to either log raw or parsed data).')
                return
        elif self.cfg['module'] == 'acs':
            self.cfg['manufacturer'] = 'WetLabs'
            try:
                # serial number in ACSParser is given in hexadecimal and preceded by 2 bytes indicating meter type
                foo = ACSParser(self.cfg['device_file']).serial_number
                if foo[:4] == '0x53':
                    self.cfg['model'] = 'ACS'
                else:
                    self.cfg['model'] = 'UnknownMeterType'
                self.cfg['serial_number'] = str(int(foo[-6:], 16))
            except:
                self.notification('Unable to parse acs device file.')
                return
            if 'log_raw' not in self.cfg.keys():
                self.cfg['log_raw'] = True
            if 'log_products' not in self.cfg.keys():
                self.cfg['log_products'] = True
        elif self.cfg['module'] == 'lisst':
            self.cfg['manufacturer'] = 'Sequoia'
            self.cfg['model'] = 'LISST'
            try:
                self.cfg['serial_number'] = str(LISSTParser(self.cfg['device_file'], self.cfg['ini_file'],
                                                            self.cfg['dcal_file'], self.cfg['zsc_file']).serial_number)
            except:
                self.notification('Unable to parse lisst device, ini, dcal, or zsc file.')
                return
            if 'log_raw' not in self.cfg.keys():
                self.cfg['log_raw'] = True
            if 'log_products' not in self.cfg.keys():
                self.cfg['log_products'] = True
        elif self.cfg['module'] == 'dataq':
            self.cfg['channels_enabled'] = []
            for c in range(4):
                if getattr(self, 'checkbox_channel%d_enabled' % (c+1)).isChecked():
                    self.cfg['channels_enabled'].append(c)
            if not self.cfg['channels_enabled']:
                self.notification('At least one channel must be enabled.', 'Nothing to log if no channels are enabled.')
                return
            if 'log_raw' not in self.cfg.keys():
                self.cfg['log_raw'] = False
            if 'log_products' not in self.cfg.keys():
                self.cfg['log_products'] = True
        # Update global instrument cfg
        if self.create:
            CFG.instruments.append(self.cfg)
            self.cfg_index = -1
        else:
            CFG.instruments[self.cfg_index] = self.cfg.copy()
        CFG.write()
        self.accept()

    @staticmethod
    def notification(message, details=None):
        msg = QtGui.QMessageBox()
        msg.setIcon(QtGui.QMessageBox.Warning)
        msg.setText(message)
        # msg.setInformativeText("This is additional information")
        if details:
            msg.setDetailedText(str(details))
        msg.setWindowTitle("Inlinino: Setup Instrument Warning")
        msg.setStandardButtons(QtGui.QMessageBox.Ok)
        msg.exec_()


class WorkerSignals(QtCore.QObject):

    status_update = QtCore.pyqtSignal(str, int)
    new_data = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()


class WorkerSerialConnection(QtCore.QObject):

    def __init__(self, port, baudrate, bytesize, parity, stopbits, timeout):
        super().__init__()
        self.alive = False
        self.signal = WorkerSignals()
        # Serial Parameters
        self.port, self.baudrate = port, baudrate
        self.bytesize, self.parity, self.stopbits, self.timeout = bytesize, parity, stopbits, timeout

    def run(self):
        print(f'Worker:run {self.port}')
        # from time import sleep
        # sleep(2)
        try:
            self.alive = True
            s = serial.Serial(self.port, self.baudrate, self.bytesize, self.parity, self.stopbits, self.timeout)
            self.signal.status_update.emit('Connected', DialogSerialConnection.STYLE_SUCCESS)
            print(f'Worker:run connected {self.port}')
            while self.alive:
                data = s.read(s.in_waiting or 1).decode('ascii', 'ignore')
                self.signal.new_data.emit(data)
        except serial.serialutil.SerialException as e:
            print(e)
            if e.errno == 16:
                self.signal.status_update.emit('Could not open port, resource busy.', DialogSerialConnection.STYLE_DANGER)
            else:
                self.signal.status_update.emit(str(e), DialogSerialConnection.STYLE_DANGER)
        except Exception as e:
            print(e)
            self.signal.status_update.emit(str(e), DialogSerialConnection.STYLE_DANGER)
        finally:
            print(f'Worker:finished {self.port}')
            self.signal.finished.emit()

    def quit(self):
        print(f'Worker:quit {self.port}')
        self.alive = False

    # def deleteLater(self) -> None:
    #     print(f'Worker:deleteLater {self.port}')
    #     super().deleteLater()

    # def __str__(self):
    #     return f'Worker {self.port} {self.alive}'


class DialogSerialConnection(QtGui.QDialog):

    STYLE_SUCCESS = 0
    STYLE_WARNING = 1
    STYLE_DANGER = 2

    def __init__(self, parent):
        super().__init__(parent)
        uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'serial_connection.ui'), self)
        instrument = parent.instrument
        # Connect buttons
        self.button_box.button(QtGui.QDialogButtonBox.Open).clicked.connect(self.accept)
        self.button_box.button(QtGui.QDialogButtonBox.Cancel).clicked.connect(self.reject)
        # Update ports list
        self.ports = list_serial_comports()
        self.ports.append(type('obj', (object,), {'device': '/dev/ttys001', 'description': 'macOS Virtual Serial'}))  # Debug macOS serial
        for p in self.ports:
            # print(f'\n\n===\n{p.description}\n{p.device}\n{p.hwid}\n{p.interface}\n{p.location}\n{p.manufacturer}\n{p.name}\n{p.pid}\n{p.product}\n{p.serial_number}\n{p.vid}')
            p_name = str(p.device)
            if p.description is not None and p.description != 'n/a':
                p_name += ' - ' + str(p.description)
            self.cb_port.addItem(p_name)
        # Set default values based on instrument
        baudrate, bytesize, parity, stopbits, timeout = '19200', '8 bits', 'none', '1', 2
        if type(instrument) == ACS:
            baudrate = str(instrument._parser.baudrate)
            timeout = 1
        elif type(instrument) == DATAQ:
            baudrate, dataq = '115200', 1
        elif type(instrument) == HyperBB:
            baudrate, timeout = '9600', 1
        elif type(instrument) == LISST:
            baudrate, timeout = '9600', 10
        elif type(instrument) == NMEA:
            baudrate, timeout = '4800', 10
        elif type(instrument) == TaraTSG:
            baudrate, timeout = '9600', 3
        self.cb_baudrate.setCurrentIndex([self.cb_baudrate.itemText(i) for i in range(self.cb_baudrate.count())].index(baudrate))
        self.cb_bytesize.setCurrentIndex([self.cb_bytesize.itemText(i) for i in range(self.cb_bytesize.count())].index(bytesize))
        self.cb_parity.setCurrentIndex([self.cb_parity.itemText(i) for i in range(self.cb_parity.count())].index(parity))
        self.cb_stopbits.setCurrentIndex([self.cb_stopbits.itemText(i) for i in range(self.cb_stopbits.count())].index(stopbits))
        self.sb_timeout.setValue(timeout)
        # Connect parameter change to active running thread
        self.cb_port.currentTextChanged.connect(self.run_serial_connection)
        self.cb_baudrate.currentTextChanged.connect(self.run_serial_connection)
        self.cb_bytesize.currentTextChanged.connect(self.run_serial_connection)
        self.cb_parity.currentTextChanged.connect(self.run_serial_connection)
        self.cb_stopbits.currentTextChanged.connect(self.run_serial_connection)
        self.sb_timeout.valueChanged.connect(self.run_serial_connection)
        # Setup Worker Object and Thread
        # self.parent = parent
        self.worker_thread = QtCore.QThread(self)
        self.worker = WorkerSerialConnection(self.port, self.baudrate, self.bytesize, self.parity, self.stopbits, self.timeout)
        self.worker.moveToThread(self.worker_thread)
        # Connect Signals
        self.worker_thread.started.connect(self.worker.run)
        self.worker_signal_connected = False
        # self.worker.finished.connect(self.worker_thread.quit)
        # self.worker.status_update.connect(self.update_status)

    def run_serial_connection(self):
        # Stop previous thread if it was running
        if self.worker_thread.isRunning():
            # Disconnect signal to prevent UI update while closing process
            self.worker.signal.new_data.disconnect(self.update_console)
            self.worker.signal.status_update.disconnect(self.update_status)
            self.worker_signal_connected = False
            # Update dialog status
            self.update_status('Disconnecting ...', self.STYLE_WARNING)
            # Attempt graceful stop worker thread
            self.worker.quit()
            print(1)
            self.worker_thread.quit()
            print(2)
            if not self.worker_thread.wait(500):
                print(3)
                # Thread did not finish, Force stop worker thread
                print(f'Force stop worker thread {self.worker.port}')
                self.worker_thread.terminate()
                print(4)
                self.worker_thread.wait()  # Required after terminate as thread might not terminate immediately
        # Update status and clear console
        self.update_status('Connecting ...', self.STYLE_WARNING)
        self.pte_console.clear()
        # Connect worker signals to Dialog
        if not self.worker_signal_connected:
            self.worker_signal_connected = True
            self.worker.signal.new_data.connect(self.update_console)
            self.worker.signal.status_update.connect(self.update_status)
        # Update Worker Configuration
        self.worker.port, self.worker.baudrate = self.port, self.baudrate
        self.worker.bytesize, self.worker.parity = self.bytesize, self.parity
        self.worker.stopbits, self.worker.timeout = self.stopbits, self.timeout
        # Start working thread
        # print(self.worker)
        self.worker_thread.start()

    def done(self, arg):
        print(f'Dialog: done {arg}')
        # Attempt graceful stop worker thread
        self.worker.quit()
        self.worker_thread.quit()
        if not self.worker_thread.wait(1000):
            # Force stop worker thread
            # print('Force stop worker thread')
            self.worker_thread.terminate()
            self.worker_thread.wait()
        super().done(arg)

    def update_console(self, data):
        self.pte_console.moveCursor(QtGui.QTextCursor.End)
        self.pte_console.insertPlainText(data)
        self.pte_console.moveCursor(QtGui.QTextCursor.StartOfLine)

    def update_status(self, status, style):
        self.label_status.setText(status)
        if style == self.STYLE_SUCCESS:
            self.label_status.setStyleSheet(f'font-weight:bold;color:#12ab29;')
        elif style == self.STYLE_WARNING:
            self.label_status.setStyleSheet(f'font-weight:normal;color:#ff9e17;')
        elif style == self.STYLE_DANGER:
            self.label_status.setStyleSheet(f'font-weight:bold;color:#e0463e;')

    # def force_worker_quit(self):
    #     if self.worker_thread.isRunning():
    #         self.worker.alive = False
    #         # self.worker.terminate()
    #         # self.worker.wait()

    @property
    def port(self) -> str:
        return self.ports[self.cb_port.currentIndex()].device

    @property
    def baudrate(self) -> int:
        return int(self.cb_baudrate.currentText())

    @property
    def bytesize(self) -> int:
        if self.cb_bytesize.currentText() == '5 bits':
            return serial.FIVEBITS
        elif self.cb_bytesize.currentText() == '6 bits':
            return serial.SIXBITS
        elif self.cb_bytesize.currentText() == '7 bits':
            return serial.SEVENBITS
        elif self.cb_bytesize.currentText() == '8 bits':
            return serial.EIGHTBITS
        raise ValueError('serial byte size not defined')

    @property
    def parity(self) -> int:
        if self.cb_parity.currentText() == 'none':
            return serial.PARITY_NONE
        elif self.cb_parity.currentText() == 'even':
            return serial.PARITY_EVEN
        elif self.cb_parity.currentText() == 'odd':
            return serial.PARITY_EVEN
        elif self.cb_parity.currentText() == 'mark':
            return serial.PARITY_MARK
        elif self.cb_parity.currentText() == 'space':
            return serial.PARITY_SPACE
        raise ValueError('serial parity not defined')

    @property
    def stopbits(self) -> int:
        if self.cb_stopbits.currentText() == '1':
            return serial.STOPBITS_ONE
        elif self.cb_stopbits.currentText() == '1.5':
            return serial.STOPBITS_ONE_POINT_FIVE
        elif self.cb_stopbits.currentText() == '2':
            return serial.STOPBITS_TWO
        raise ValueError('serial stop bits not defined')

    @property
    def timeout(self) -> int:
        return int(self.sb_timeout.value())


class DialogSocketConnection(QtGui.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'socket_connection.ui'), self)
        # Connect buttons
        self.button_box.button(QtGui.QDialogButtonBox.Open).clicked.connect(self.accept)
        self.button_box.button(QtGui.QDialogButtonBox.Cancel).clicked.connect(self.reject)

    @property
    def ip(self) -> str:
        return self.le_ip.text()

    @property
    def port(self) -> int:
        return int(self.sb_port.value())

    # @property
    # def timeout(self) -> int:
    #     return int(self.sb_timeout.value())


class DialogLoggerOptions(QtGui.QDialog):
    def __init__(self, parent):
        super().__init__(parent)  #, QtCore.Qt.WindowStaysOnTopHint
        uic.loadUi(os.path.join(PATH_TO_RESOURCES, 'logger_options.ui'), self)
        self.le_prefix_custom_connected = False
        self.instrument = parent.instrument
        # Logger Options
        self.le_log_path.setText(self.instrument.log_get_path())
        self.button_browse_log_directory.clicked.connect(self.act_browse_log_directory)
        self.update_filename_template()
        # Connect Prefix Checkbox to update Filename Template
        self.cb_prefix_diw.toggled.connect(self.update_filename_template)
        self.cb_prefix_fsw.toggled.connect(self.update_filename_template)
        self.cb_prefix_dark.toggled.connect(self.update_filename_template)
        self.cb_prefix_custom.toggled.connect(self.update_filename_template)
        # Connect buttons
        self.button_box.button(QtGui.QDialogButtonBox.Save).setDefault(True)
        self.button_box.button(QtGui.QDialogButtonBox.Save).clicked.connect(self.accept)
        self.button_box.button(QtGui.QDialogButtonBox.Cancel).clicked.connect(self.reject)

    @property
    def cover_log_prefix(self) -> str:
        prefix = ''
        if self.cb_prefix_diw.isChecked():
            prefix += 'DIW'
        if self.cb_prefix_fsw.isChecked():
            prefix += 'FSW'
        if self.cb_prefix_dark.isChecked():
            prefix += 'DARK'
        if self.cb_prefix_custom.isChecked():
            if not self.le_prefix_custom_connected:
                self.le_prefix_custom.textChanged.connect(self.update_filename_template)
                self.le_prefix_custom_connected = True
            prefix += self.le_prefix_custom.text()
        elif self.le_prefix_custom_connected:
            self.le_prefix_custom.textChanged.disconnect(self.update_filename_template)
            self.le_prefix_custom_connected = False
        if prefix:
            prefix += '_'
        # Check All required fields are complete
        return prefix

    @property
    def log_path(self) -> str:
        return self.le_log_path.text()

    def act_browse_log_directory(self):
        self.le_log_path.setText(QtGui.QFileDialog.getExistingDirectory(caption='Choose logging directory',
                                                     directory=self.le_log_path.text()))
        self.show()

    def update_filename_template(self):
        # self.le_filename_template.setText(instrument.log_get_filename())  # Not up to date
        self.le_filename_template.setText(self.cover_log_prefix + self.instrument.bare_log_prefix +
                                          '_YYYYMMDD_hhmmss.' + self.instrument.log_get_file_ext())


class App(QtGui.QApplication):
    def __init__(self, *args):
        QtGui.QApplication.__init__(self, *args)
        self.splash_screen = QtGui.QSplashScreen(QtGui.QPixmap(os.path.join(PATH_TO_RESOURCES, 'inlinino.ico')))
        self.splash_screen.show()
        self.main_window = MainWindow()
        self.startup_dialog = DialogStartUp()
        self.splash_screen.close()

    def start(self, instrument_index=None):
        if not instrument_index:
            logger.debug('Startup Dialog')
            self.startup_dialog.show()
            act = self.startup_dialog.exec_()
            if act == self.startup_dialog.LOAD_INSTRUMENT:
                instrument_index = self.startup_dialog.selection_index
            elif act == self.startup_dialog.SETUP_INSTRUMENT:
                setup_dialog = DialogInstrumentSetup(
                    self.startup_dialog.instruments_to_setup[self.startup_dialog.selection_index])
                setup_dialog.show()
                if setup_dialog.exec_():
                    instrument_index = setup_dialog.cfg_index
                else:
                    logger.info('Setup closed')
                    self.start()  # Restart application to go back to startup screen
            else:
                logger.info('Startup closed')
                sys.exit()

        # Load instrument
        instrument_name = CFG.instruments[instrument_index]['model'] + ' ' \
                          + CFG.instruments[instrument_index]['serial_number']
        instrument_module_name = CFG.instruments[instrument_index]['module']
        logger.debug('Loading instrument ' + instrument_name)
        instrument_loaded = False
        while not instrument_loaded:
            try:
                if instrument_module_name == 'generic':
                    self.main_window.init_instrument(Instrument(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'acs':
                    self.main_window.init_instrument(ACS(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'dataq':
                    self.main_window.init_instrument(DATAQ(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'hyperbb':
                    self.main_window.init_instrument(HyperBB(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'lisst':
                    self.main_window.init_instrument(LISST(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'nmea':
                    self.main_window.init_instrument(NMEA(instrument_index, InstrumentSignals()))
                elif instrument_module_name == 'taratsg':
                    self.main_window.init_instrument(TaraTSG(instrument_index, InstrumentSignals()))
                else:
                    logger.critical('Instrument module not supported')
                    sys.exit(-1)
                instrument_loaded = True
            except Exception as e:
                raise e
                logger.warning('Unable to load instrument.')
                logger.warning(e)
                self.closeAllWindows()  # ACS, HyperBB, and LISST are opening pyqtgraph windows
                # Dialog Box
                setup_dialog = DialogInstrumentSetup(instrument_index)
                setup_dialog.show()
                setup_dialog.notification('Unable to load instrument. Please check configuration.', e)
                if setup_dialog.exec_():
                    logger.info('Updated configuration')
                else:
                    logger.info('Setup closed')
                    self.start()  # Restart application to go back to startup screen
        # Start Main Window
        self.main_window.show()
        sys.exit(self.exec_())
