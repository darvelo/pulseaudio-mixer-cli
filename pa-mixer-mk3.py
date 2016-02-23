#!/usr/bin/env python3

import itertools as it, operator as op, functools as ft
from collections import OrderedDict
from contextlib import contextmanager
import os, sys, re, time, logging, configparser
import base64, hashlib, unicodedata
import signal, threading

from pulsectl import Pulse, PulseLoopStop


class LogMessage(object):
	def __init__(self, fmt, a, k): self.fmt, self.a, self.k = fmt, a, k
	def __str__(self): return self.fmt.format(*self.a, **self.k) if self.a or self.k else self.fmt

class LogStyleAdapter(logging.LoggerAdapter):
	def __init__(self, logger, extra=None):
		super(LogStyleAdapter, self).__init__(logger, extra or {})
	def log(self, level, msg, *args, **kws):
		if not self.isEnabledFor(level): return
		log_kws = {} if 'exc_info' not in kws else dict(exc_info=kws.pop('exc_info'))
		msg, kws = self.process(msg, kws)
		self.logger._log(level, LogMessage(msg, args, kws), (), log_kws)

class LogPrefixAdapter(LogStyleAdapter):
	def __init__(self, logger, prefix, extra=None):
		if isinstance(logger, str): logger = get_logger(logger)
		if isinstance(logger, logging.LoggerAdapter): logger = logger.logger
		super(LogPrefixAdapter, self).__init__(logger, extra or {})
		self.prefix = prefix
	def process(self, msg, kws):
		super(LogPrefixAdapter, self).process(msg, kws)
		return '[{}] {}'.format(self.prefix, msg), kws

get_logger = lambda name: LogStyleAdapter(logging.getLogger(name))


def uid_str( seed=None, length=4,
		_seed_gen=it.chain.from_iterable(map(range, it.repeat(2**30))) ):
	seed_bytes = length * 6 // 8
	assert seed_bytes * 8 // 6 == length, [length, seed_bytes]
	if seed is None: seed = '\0\0\0{:08x}'.format(next(_seed_gen))
	seed = hashlib.sha256(bytes(seed, encoding='utf-8')).digest()[:seed_bytes]
	return base64.urlsafe_b64encode(seed).decode()


class Conf(object):
	def __repr__(self): return repr(vars(self))

	adjust_step = 5 # percent, 0-100
	# Volume values are relative to "normal" (non-soft-boosted) pulseaudio volume
	max_volume = 1.0 # relative value, displayed as "100%"
	min_volume = 0.01 # relative value, displayed as "0%"

	use_device_name = False
	use_media_name = False
	placeholder_media_names = 'audio stream', 'AudioStream', 'Output', 'ALSA Playback'
	name_len_max = 100
	name_cut_from = 'left' # "left" or "right"
	name_show_level = True

	overkill_redraw = False # if terminal gets resized often, might cause noticeable flickering
	verbose = False

	stream_params = None
	broken_chars_replace = '_'
	focus_default = 'first' # either "first" or "last"
	focus_new_items = True
	focus_new_items_delay = 5.0 # min seconds since last focus change to trigger this

	@staticmethod
	def parse_bool(val, _states={
			'1': True, 'yes': True, 'true': True, 'on': True,
			'0': False, 'no': False, 'false': False, 'off': False }):
		try: return _states[val.lower()]
		except KeyError: raise ValueError(val)


def update_conf_from_file(conf, path_or_file):
	if isinstance(path_or_file, str): path_or_file = open(path_or_file)
	with path_or_file as src:
		config = configparser.RawConfigParser(allow_no_value=True)
		config.readfp(src)

	for k in dir(conf):
		if k.startswith('_'): continue
		v = getattr(conf, k)
		if isinstance(v, str): get_val = lambda *a: str(config.get(*a))
		elif isinstance(v, bool): get_val = config.getboolean
		elif isinstance(v, int): get_val = config.getint
		elif isinstance(v, float): get_val = lambda *a: float(config.get(*a))
		else: continue # values with other types cannot be specified in config
		for k_conf in k, k.replace('_', '-'):
			try: setattr(conf, k, get_val('default', k_conf))
			except configparser.Error: pass

	conf.stream_params = OrderedDict(conf.stream_params or dict())
	for sec in config.sections():
		if not re.search(r'^stream\b.', sec): continue
		params = list()
		for k, v in config.items(sec):
			match = re.search(r'^(match|equals)\[(.*)\]$', k)
			if match:
				v = re.compile(r'^{}$'.format(re.escape(v)) if match.group(1) == 'equals' else v)
				params.append(('match', match.group(2), v))
			else: params.append(('set', k, v))
		conf.stream_params[sec] = params



class PAMixerMenuItem(object):

	def __init__(self, menu, obj_t, obj_id, obj):
		self.menu, self.conf = menu, menu.conf
		self.t, self.uid, self.obj = obj_t, obj_id, obj
		self.hidden, self.created_ts = False, time.monotonic()
		self.update_name()

		if self.conf.dump_stream_params:
			from pprint import pprint
			dump = OrderedDict(uid=self.uid, name=self.name)
			dump['props'] = sorted(self.obj.proplist.items())
			pprint(dump, sys.stderr)

	def __repr__(self):
		return '<{}[{:x}] {}[{}]: {}>'.format(
			self.__class__.__name__, id(self), self.t, self.uid, self.name )


	def update_name(self):
		self.name = self._get_name()
		if not self.name: self.name = self._get_name_unique('null')

	def _strip_noise_bytes(self, obj, replace='_'):
		'''Make sure there arent any random weird chars that dont belong to any alphabet.
			Only ascii non-letters are allowed, as fancy symbols don't seem to work well with curses.'''
		if not isinstance(obj, str): obj = str(obj)
		obj_ucs = list()
		for uc in obj:
			try:
				unicodedata.name(uc)
				if unicodedata.category(uc) != 'Ll': uc.encode('ascii')
			except (ValueError, UnicodeEncodeError):
				if replace: obj_ucs.append(replace)
			else: obj_ucs.append(uc)
		return ''.join(obj_ucs)

	def _get_name_unique(self, name):
		return '{} #{}'.format(name, uid_str())

	def _get_name_descriptive(self):
		'Can probably fail with KeyError if something is really wrong with stream/device props.'
		ext, props = None, dict(
			(k, self._strip_noise_bytes(v, self.conf.broken_chars_replace))
			for k, v in self.obj.proplist.items() )

		if self.t == 'stream':
			if self.conf.use_media_name:
				name = props.get('media.name')
				if name and name not in self.conf.placeholder_media_names: return name
			try: name = props['application.name']
			except KeyError: # some synthetic stream with non-descriptive name
				name = self._get_name_unique(props['media.name'])
			ext = '({application.process.user}@'\
				'{application.process.host}:{application.process.id})'

		elif self.t == 'sink':
			if self.conf.use_device_name: name = self.obj.name
			else:
				name = props.get('alsa.id')\
					or props.get('device.description') or props.get('device.api')
				if not name:
					try: name = '{}.{}'.format(props['device.api'], props['device.string'])
					except KeyError:
						self._get_name_unique(props['device.description'])
				ext = '({device.profile.name}@{alsa.driver_name})'

		else: raise KeyError('Unknown menu-item type (for naming): {}'.format(self.t))

		if ext:
			try:
				name = '{} {}'.format( name,
					re.sub(r'\{([^}]+)\}', r'{}', ext).format(
						*op.itemgetter(*re.findall(r'\{([^}]+)\}', ext))(props) ) )
			except KeyError as err:
				log.debug( 'Unable to get extended descriptive name'
					' (type: {!r}, uid: {}) due to missing key: {}', self.t, self.uid, err )
		return name

	def _get_name(self):
		try: return self._get_name_descriptive()
		except Exception as err:
			if self.menu.fatal: raise
			log.info('Failed to get descriptive name for {!r} ({}): {}', self.t, self.uid, err)
		return self._get_name_unique(self.t)


	@property
	def muted(self):
		return bool(self.obj.mute)
	@muted.setter
	def muted(self, val):
		self.obj.mute = int(val)
		with self.menu.update_wakeup() as pulse: pulse.mute(self.obj, self.obj.mute)

	@property
	def volume(self):
		'Volume as one float in 0-1 range.'
		return min(1.0, max(0,
			self.obj.volume.value_flat - self.conf.min_volume ) / float(self.conf.max_volume))
	@volume.setter
	def volume(self, val):
		val_pulse = min(1.0, max(0, val)) * self.conf.max_volume + self.conf.min_volume
		log.debug('Setting volume: {} (pulse: {}) for {}', val, val_pulse, self)
		with self.menu.update_wakeup() as pulse: pulse.volume_set_all_chans(self.obj, val_pulse)

	@property
	def port(self):
		if self.t != 'sink': return
		return self.obj.active_port
	@port.setter
	def port(self, name):
		if self.t != 'sink':
			raise PAMixerInvalidAction(( 'Setting ports is only'
				' valid for {!r}-type streams, not {!r}-type' ).format('sink', self.t))
		# XXX: implement port setting


	def muted_toggle(self): self.muted = not self.muted
	def volume_change(self, delta):
		log.debug('Volume update: {} -> {} [{}]', self.volume, self.volume + delta, delta)
		self.volume += delta

	def get_next(self): return self.menu.item_after(self)
	def get_prev(self): return self.menu.item_before(self)


class PAMixerMenu(object):

	focus_policies = dict(first=op.itemgetter(0), last=op.itemgetter(-1))

	def __init__(self, pulse, conf=None, fatal=False):
		self.pulse, self.fatal, self.conf = pulse, fatal, conf or Conf()
		self.items, self.item_objs = list(), OrderedDict()
		self._update_lock = self._update_signal = False
		self._pulse_hold, self._pulse_lock = threading.Lock(), threading.Lock()

	def update(self):
		self._update_lock = self._update_signal = True
		while self._update_signal:
			self._update_signal = False

			obj_new, obj_gone = set(), set(self.item_objs)
			with self.update_wakeup() as pulse:
				for obj_t, obj_list_func in\
						[('sink', pulse.sink_list), ('stream', pulse.sink_input_list)]:
					for obj in obj_list_func():
						obj_id = '{}-{}'.format(obj_t, obj.index)
						if obj_id not in self.item_objs:
							obj_new.add(obj_id)
							self.item_objs[obj_id] = PAMixerMenuItem(self, obj_t, obj_id, obj)
						else: obj_gone.remove(obj_id)
			for obj_id in obj_gone: del self.item_objs[obj_id]
			for obj_id in obj_new: self.apply_stream_params(self.item_objs[obj_id])

			# Sort sinks to be always on top
			sinks, streams, ordered = list(), list(), True
			for obj_id, item in self.item_objs.items():
				if item.t == 'sink':
					if streams: ordered = False
					sinks.append((obj_id, item))
				else: streams.append((obj_id, item))
			if not ordered:
				self.item_objs.clear()
				for obj_id, item in it.chain(sinks, streams): self.item_objs[obj_id] = item

			self.items = list(item for item in self.item_objs.values() if not item.hidden)
		self._update_lock = False

	@contextmanager
	def update_wakeup_poller( self, wakeup_handler,
			wakeup_pid=None, wakeup_sig=signal.SIGUSR1 ):
		if wakeup_pid is None: wakeup_pid = os.getpid()
		signal.signal(wakeup_sig, wakeup_handler)
		poller_thread = None
		def ev_cb(ev):
			log.debug('pulsectl event: {} {}', ev.facility, ev.index)
			if threading.current_thread() is poller_thread: os.kill(wakeup_pid, wakeup_sig)
			else: wakeup_handler()
		def poller():
			nonlocal poller_thread
			poller_thread = threading.current_thread()
			self.pulse.event_mask_set('all')
			self.pulse.event_callback_set(ev_cb)
			while True:
				with self._pulse_hold: self._pulse_lock.acquire() # ...threads ;(
				try: self.pulse.event_listen()
				finally: self._pulse_lock.release()
		try: yield poller
		finally:
			# time.sleep(0.5)
			self.pulse.event_listen_stop()

	@contextmanager
	def update_wakeup(self, loop_interval=0.03):
		'Anything pulse-related MUST be done in this context.'
		with self._pulse_hold:
			for n in range(int(5.0 / loop_interval)):
				# wakeup only works when loop is actually started,
				#  which might not be the case regardless of any locks.
				self.pulse.event_listen_stop()
				if self._pulse_lock.acquire(timeout=loop_interval): break
			else:
				raise RuntimeError('poll_wakeup() hangs, likely locking issue')
			try: yield self.pulse
			finally: self._pulse_lock.release()

	def update_wakeup_handler(self, sig=None, frm=None):
		# XXX: do less than full refresh here
		self._update_signal = True

	def apply_stream_params(self, item):
		for sec, checks in (self.conf.stream_params or dict()).items():
			match, params = True, OrderedDict()
			for t, k, v in checks:
				if t == 'match':
					if match and not v.search(item.obj.proplist.get(k, '')): match = False
				elif t == 'set': params[k] = v
				else: raise ValueError((t, k, v))
			if match:
				log.debug( 'Matched stream {!r} (name: {!r})'
					' to config section: {}', item, item.name, sec )
				for k, v in params.items():
					m = re.search(r'^volume-(min|max|set)$', k)
					if m:
						vol = float(v)
						if m.group(1) == 'max':
							if item.volume > vol: item.volume = vol
						elif m.group(1) == 'min':
							if item.volume < vol: item.volume = vol
						elif m.group(1) == 'set': item.volume = vol
					elif k == 'hidden': item.hidden = self.conf.parse_bool(v)
					elif k == 'port':
						try: item.port = v
						except PAMixerInvalidAction as err:
							log.error( 'Unable to set port for stream {!r}'
								' (name: {!r}, config section: {}): {}', item, item.name, sec, err )
					else:
						log.debug( 'Unrecognized stream'
							' parameter (section: {!r}): {!r} (value: {!r})', sec, k, v )

	@property
	def item_list(self):
		self.update()
		return self.items

	def item_default(self):
		if not self.items: return
		func = self.focus_policies[self.conf.focus_default]
		return func(self.items)

	def item_newer(self, ts):
		items = sorted(self.items, key=op.attrgetter('created_ts'), reverse=True)
		if items and items[0].created_ts > ts: return items[0]

	def item_after(self, item=None):
		if item:
			for item2 in self.items:
				if item is StopIteration: return item2
				if item2.uid == item.uid: item = StopIteration
		return self.item_default()

	def item_before(self, item=None):
		if item:
			item_prev = None
			for item2 in self.items:
				if item2.uid == item.uid:
					if not item_prev: break
					return item_prev
				item_prev = item2
		return self.item_default()


class PAMixerUI(object):

	item_len_min = 10
	bar_len_min = 10
	bar_caps_func = staticmethod(lambda bar='': ' [ ' + bar + ' ]')
	border = 1
	name_cut_funcs = dict(left=lambda n,c: n[max(0, len(n) - c):], right=lambda n,c: n[:c])

	def __init__(self, menu):
		self.menu, self.conf = menu, menu.conf

	def __enter__(self):
		self.c = None
		return self

	def __exit__(self, exc_t, exc_val, exc_tb):
		if self.c:
			self.c.endwin()
			self.c = None


	def c_win_init(self):
		# Used to create a window with borders here,
		#  but these borders don't seem to be cleared properly.
		# So using stdscr now, and painting borders in the app.
		win = self.c_stdscr
		win.keypad(True)
		win.bkgdset(' ')
		return win

	def c_win_size(self, win):
		'Returns "nlines, ncols, begin_y, begin_x", taking border into account.'
		size = win.getmaxyx()
		nlines, ncols = max(1, size[0] - 2 * self.border), max(1, size[1] - 2 * self.border)
		return nlines, ncols, min(self.border, size[0]), min(self.border, size[1])

	def c_win_draw(self, win, items, item_hl):
		win.erase()
		if not items: return

		win_rows, win_len, pad_x, pad_y = self.c_win_size(win)
		if win_len <= 1: return # nothing fits

		# Fit stuff vertically
		if win_rows < len(items) + 1: # pick/display items near highlighted one
			pos, offset = items.index(item_hl), 1
			items, items_fit = dict(enumerate(items)), {pos: items[pos]}
			while True:
				ps = list(p for p in [pos + offset, pos - offset] if p in items)
				if not ps: break
				for p in ps:
					items_fit[p] = items[p]
					if win_rows <= len(items_fit) + 1: break
				else:
					offset += 1
					continue
				break
			items = map(op.itemgetter(1), sorted(items_fit.items(), key=op.itemgetter(0)))

		# Fit stuff horizontally
		mute_button_len, level_len = 2, 5
		item_len_max = max(len(item.name) for item in items)
		if self.conf.name_show_level: item_len_max += level_len
		if self.conf.name_len_max:
			item_len_max = min(item_len_max, self.conf.name_len_max)
		bar_len = win_len - item_len_max - mute_button_len - len(self.bar_caps_func())
		if bar_len < self.bar_len_min:
			item_len_max = max(self.item_len_min, item_len_max + bar_len - self.bar_len_min)
			bar_len = win_len - item_len_max - mute_button_len - len(self.bar_caps_func())
			if bar_len <= 0: item_len_max = win_len # just draw labels
			if item_len_max < self.item_len_min: item_len_max = max(len(item.name) for item in items)

		for row, item in enumerate(items):
			if row >= win_rows - 1: break # not sure why bottom window row seem to be unusable
			row += pad_y

			attrs = self.c.A_REVERSE if item is item_hl else self.c.A_NORMAL
			name_len = item_len_max - bool(self.conf.name_show_level) * level_len
			name = self.name_cut_funcs[self.conf.name_cut_from](item.name, name_len)

			if self.conf.name_show_level:
				level = max(0, min(100, int(round(item.volume * 100))))
				if level == 0: level = '--'
				elif level == 100: level = '++'
				else: level = '{:>2d}'.format(level)
				name = '[{}] {}'.format(level, name)

			win.addstr(row, 0, ' ' * pad_x)
			win.addstr(row, pad_x, name, attrs)
			item_name_end = item_len_max + pad_x
			if win_len > item_name_end + mute_button_len:
				if item.muted: mute_button = ' M'
				else: mute_button = ' -'
				win.addstr(row, item_name_end, mute_button)

				if bar_len > 0:
					bar_fill = int(round(item.volume * bar_len))
					bar = self.bar_caps_func('#' * bar_fill + '-' * (bar_len - bar_fill))
					win.addstr(row, item_name_end + mute_button_len, bar)

	def c_key(self, k):
		if len(k) == 1: return ord(k)
		return getattr(self.c, 'key_{}'.format(k).upper())


	_item_hl = _item_hl_ts = None

	@property
	def item_hl(self):
		if self._item_hl and self.conf.focus_new_items:
			ts = self._item_hl_ts
			if ts: ts += self.conf.focus_new_items_delay or 0
			item = self.menu.item_newer(ts)
			if item: self._item_hl = item
		return self._item_hl

	@item_hl.setter
	def item_hl(self, item):
		self._item_hl, self._item_hl_ts = item, time.monotonic()


	def _run(self, stdscr):
		c, self.c_stdscr = self.c, stdscr
		key_match = lambda key,*choices: key in map(self.c_key, choices)

		c.curs_set(0)
		c.use_default_colors()

		win = self.c_win_init()
		self.conf.adjust_step /= 100.0

		while True:
			# XXX: full refresh on every keypress is a bit excessive
			# XXX: pulsectl error handling here?
			items, item_hl = self.menu.item_list, self.item_hl
			if item_hl is None: item_hl = self.item_hl = self.menu.item_default()
			if item_hl not in items: item_hl = self.menu.item_default()
			self.c_win_draw(win, items, item_hl)

			key = None
			while True:
				try: key = win.getch()
				except KeyboardInterrupt: key = self.c_key('q')
				except c.error: break
				try: key_name = c.keyname(key)
				except ValueError: key_name = 'unknown' # e.g. "-1"
				break
			if key is None: continue
			log.debug('Keypress event: {} ({!r})', key, key_name)

			if item_hl:
				if key_match(key, 'up', 'k', 'p'): self.item_hl = item_hl.get_prev()
				elif key_match(key, 'down', 'j', 'n'): self.item_hl = item_hl.get_next()
				elif key_match(key, 'left', 'h', 'b'):
					item_hl.volume_change(-self.conf.adjust_step)
				elif key_match(key, 'right', 'l', 'f'): item_hl.volume_change(self.conf.adjust_step)
				elif key_match(key, ' ', 'm'): item_hl.muted_toggle()
				elif key_name.isdigit(): # 1-0 keyboard row
					item_hl.volume = (float(key_name) or 10.0) / 10 # 0 is 100%

			if key_match(key, 'resize', '\f'):
				if self.conf.overkill_redraw:
					c.endwin()
					stdscr.refresh()
					win = self.c_win_init()
				else:
					win.resize(*win.getmaxyx())
			elif key_match(key, 'q'): break

	def run(self):
		import locale, curses # has a ton of global state
		locale.setlocale(locale.LC_ALL, '') # see top of "curses" module doc for rationale
		self.c = curses
		self.c.wrapper(self._run)


def main(args=None):
	conf = Conf()
	conf_file = os.path.expanduser('~/.pulseaudio-mixer-cli.cfg')
	try: conf_file = open(conf_file)
	except (OSError, IOError) as err: pass
	else: update_conf_from_file(conf, conf_file)

	import argparse
	parser = argparse.ArgumentParser(description='Command-line PulseAudio mixer tool.')

	parser.add_argument('-a', '--adjust-step',
		action='store', type=int, metavar='step', default=conf.adjust_step,
		help='Adjustment for a single keypress in interactive mode (0-100%%, default: %(default)s%%).')
	parser.add_argument('-l', '--max-level',
		action='store', type=float, metavar='volume', default=conf.max_volume,
		help='Relative volume level to treat as max (default: %(default)s).')
	parser.add_argument('-n', '--use-media-name',
		action='store_true', default=conf.use_media_name,
		help='Display streams by "media.name" property, if possible.'
			' Default is to prefer application name and process properties.')

	parser.add_argument('-v', '--verbose',
		action='store_true', default=conf.verbose,
		help='Dont close stderr to see any sort of errors (which'
			' mess up curses interface, thus silenced that way by default).')
	parser.add_argument('--dump-stream-params',
		action='store_true', help='Dump all parameters for each stream to stderr.')
	parser.add_argument('--debug', action='store_true', help='Verbose operation mode.')
	parser.add_argument('--debug-pipes', action='store_true',
		help='Also logs chatter between parent/child pids. Very noisy, only useful with --debug.')
	parser.add_argument('--fatal', action='store_true',
		help='Dont try too hard to recover from errors. For debugging purposes only.')

	args = sys.argv[1:] if args is None else args
	opts = parser.parse_args(args)

	for k,v in vars(opts).items(): setattr(conf, k, v)
	del opts

	global log, print
	logging.basicConfig(
		level=logging.DEBUG if conf.debug else logging.WARNING,
		format='%(asctime)s :: %(threadName)s %(levelname)s :: %(message)s',
		datefmt='%Y-%m-%d %H:%M:%S' )
	log = get_logger('main')
	print = ft.partial(print, file=sys.stderr, flush=True) # stdout is used by curses
	log.debug('Initializing...')

	with Pulse('pa-mixer-mk3') as pulse:
		menu = PAMixerMenu(pulse, conf, fatal=conf.fatal)
		wakeup_pid = os.getpid()

		with menu.update_wakeup_poller(menu.update_wakeup_handler) as poller:
			log.debug('Starting pulsectl event poller thread...')
			threading.Thread(target=poller, name='pulsectl', daemon=True).start()
			with PAMixerUI(menu) as curses_ui:
				# Any output will mess-up curses ui, so try to close sys.stderr if possible
				if not conf.verbose and not conf.debug\
					and not conf.dump_stream_params: sys.stderr.close()
				log.debug('Entering curses ui loop...')
				curses_ui.run()
				log.debug('Finished')

if __name__ == '__main__': sys.exit(main())