#!/usr/bin/env python

#
# RPM Build Bot 2
#
# Author: Dmitriy Kuminov <coding@dmik.org>
#
# This file is provided AS IS with NO WARRANTY OF ANY KIND, INCLUDING THE
# WARRANTY OF DESIGN, MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE.
#

VERSION = '0.1'


RPMBUILD_EXE = 'rpmbuild.exe'
RPM2CPIO_EXE = 'rpm2cpio.exe'
CPIO_EXE = 'cpio.exe'


SCRIPT_INI_FILE = 'rpmbuild-bot2.ini'
SCRIPT_LOG_FILE = 'rpmbuild-bot2.log'

DATETIME_FMT = '%Y-%m-%d %H:%M:%S'

VER_FULL_REGEX = '\d+[.\d]*-\w+[.\w]*\.\w+'
BUILD_USER_REGEX = '[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+'


import sys, os, re, copy, argparse, ConfigParser, subprocess, datetime, traceback, shutil, time
import getpass, socket # for user and hostname


#
# -----------------------------------------------------------------------------
#
# Overrides ConfigParser to provide improved INI file reading by adding support
# for the Python 3 `${section.option}` interpolation flavor. The idea is taken
# from https://stackoverflow.com/a/35877548. Also adds the following extensions:
#
# - #getlist that interprets the option's value as list of strings separated by
#   the given separator, and #getlines and #getwords shortcuts.
# - Support for `${ENV:<NAME>}` interpolation that is replaced with the contents
#   of the <NAME> environment variable.
# - Support for `${SHELL:<COMMAND>}` interpolation that is replaced with the
#   standard output of <COMMAND> run by the shell.
# - Support for `${RPM:<NAME>}` interpolation that is replaced with the value
#   of the <NAME> RPM macro.
# - Support for copy.deepcopy.
#

class Config (ConfigParser.SafeConfigParser):

  def __init__ (self, rpm_macros, *args, **kwargs):

    self.get_depth = 0
    self.rpm_macros = rpm_macros
    ConfigParser.SafeConfigParser.__init__ (self, *args, **kwargs)

  def __deepcopy__ (self, memo):

    copy = Config (self.rpm_macros, defaults = self.defaults ())
    copy.rpm_macros = self.rpm_macros
    for s in self.sections ():
      copy.add_section (s)
      for (n, v) in self.items (s):
        copy.set (s, n, v)
    return copy

  def get (self, section, option = None, raw = False, vars = None):

    if not option:
      section, option = section.split (':')

    ret = ConfigParser.SafeConfigParser.get (self, section, option, True, vars)
    if raw:
      return ret

    for f_section, f_option in re.findall (r'\$\{(\w+:)?((?<=SHELL:).+|\w+)\}', ret):
      self.get_depth = self.get_depth + 1
      if self.get_depth < ConfigParser.MAX_INTERPOLATION_DEPTH:
        try:
          if f_section == 'ENV:':
            sub = os.environ.get (f_option)
            if not sub: raise ConfigParser.NoOptionError, (f_option, f_section [:-1])
          elif f_section == 'SHELL:':
            sub = shell_output (f_option).strip ()
          elif f_section == 'RPM:':
            if f_option not in self.rpm_macros:
              sub = command_output ([RPMBUILD_EXE, '--eval', '%%{?%s}' % f_option]).strip ()
              self.rpm_macros [f_option] = sub
            else:
              sub = self.rpm_macros [f_option]
          else:
            sub = self.get (f_section [:-1] or section, f_option, vars = vars)
          ret = ret.replace ('${{{0}{1}}}'.format (f_section, f_option), sub)
        except RunError as e:
          raise ConfigParser.InterpolationError (section, option,
            'Failed to interpolate ${%s%s}:\nThe following command failed with: %s:\n  %s' % (f_section, f_option, e.msg, e.cmd))
      else:
        raise ConfigParser.InterpolationDepthError (option, section, ret)

    self.get_depth = self.get_depth - 1
    return ret

  def getlist (self, section, option = None, sep = None):
    return filter (None, self.get (section, option).split (sep))

  def getlines (self, section, option = None): return self.getlist (section, option, '\n')

  def getwords (self, section, option = None): return self.getlist (section, option, None)


#
# -----------------------------------------------------------------------------
#
# Generic error exception for this script.
#
# If both prefix and msg are not None, then prefix followed by a colon is
# prepended to msg. Otherwise prefix is considered empty and either of them
# which is not None is treated as msg. The hint argument, if not None,
# specifies recommendations on how to fix the error. Note that hint must always
# go as a third argument (or be passed by name).
#

class Error (BaseException):
  code = 101
  def __init__ (self, prefix, msg = None, hint = None):
    self.prefix = prefix if msg else None
    self.msg = msg if msg else prefix
    self.hint = hint
    BaseException.__init__ (self, (self.prefix and self.prefix + ': ' or '') + self.msg)


#
# -----------------------------------------------------------------------------
#
# Error exception for #run and #run_pipe functions. See Error for more info.
#

class RunError (Error):
  code = 102
  def __init__ (self, cmd, msg, hint = None, log_file = None):
    self.cmd = cmd
    self.log_file = log_file
    Error.__init__ (self, msg, hint = hint)


#
# -----------------------------------------------------------------------------
#
# Returns a human readable string of float unix_time in local time zone.
#

def to_localtimestr (unix_time):
  return time.strftime ('%Y-%m-%d %H:%M:%S %Z', time.localtime (unix_time))


#
# -----------------------------------------------------------------------------
#
# Returns a human readable string of float unix_time in UTC.
#

def to_unixtimestr (unix_time):
  return time.strftime ('%Y-%m-%d %H:%M:%S UTC', time.gmtime (unix_time))

#
# -----------------------------------------------------------------------------
#
# Logs a message to the console and optionally to a file.
#
# If msg doesn't end with a new line terminator, it will be appended.
#

def log (msg):

  if msg [-1] != '\n':
    msg += '\n'

  if g_output_file:

    g_output_file.write (msg)

    # Note: obey log_to_console only if the console is not redirected to a file.
    if g_args.log_to_console and g_log != sys.stdout:
      sys.stdout.write (msg)

  else:

    sys.stdout.write (msg)

    # Note: log to the file only when the console is not redirected to it.
    if g_log and g_log != sys.stdout:
      g_log.write (msg)


#
# -----------------------------------------------------------------------------
#
# Same as log but prepends a string in kind followed by a colon to the message.
#
# The kind string should indicate the message kind (ERROR, HINT, INFO etc). If
# msg is None, prefix will be treated as msg. Otherwise, prefix will be put
# between kind and msg, followed by a colon. If the resulting message doesn't
# end with one of `.:!?`, a dot will be appended.
#

def log_kind (kind, prefix, msg = None):

  if not msg:
    msg = prefix
    prefix = None

  if not msg [-1] in '.:!?':
    msg += '.'
  log ('%s: ' % kind + (prefix and prefix + ': ' or '') + msg)


def log_err (prefix, msg = None):
  return log_kind ('ERROR', prefix, msg)


def log_note (prefix, msg = None):
  return log_kind ('NOTE', prefix, msg)


def log_hint (prefix, msg = None):
  return log_kind ('HINT', prefix, msg)


#
# -----------------------------------------------------------------------------
#
# Prepare a log file by backing up the previous one.
#

def rotate_log (log_file):

  if os.path.exists (log_file):

    try: os.remove (log_file + '.bak')
    except OSError: pass

    try: os.rename (log_file, log_file + '.bak')
    except OSError as e:
      raise Error ('Cannot rename `%(log_file)s` to `.bak`: %(e)s' % locals ())

#
# -----------------------------------------------------------------------------
#
# Ensures path is a directory and exists.
#

def ensure_dir (path):

  try:
    if not os.path.isdir (path):
      os.makedirs (path)
  except OSError as e:
    raise Error ('Cannot create directory `%(path)s`: %(e)s' % locals ())


#
# -----------------------------------------------------------------------------
#
# Removes a file or a directory (including its contents) at path. Does not raise
# an exception if the path does not exist.
#

def remove_path (path):

  try:
    if os.path.isfile (path):
      os.remove (path)
    elif os.path.isdir (path):
      shutil.rmtree (path)
  except OSError as e:
    if e.errno != 2:
      raise


#
# -----------------------------------------------------------------------------
#
# Runs a command in a separate process and captures its output.
#
# This is a simplified shortcut to subprocess.check_output that raises RunError
# on failure. Command must be a list where the first entry is the name of the
# executable.
#

def command_output (command):
  try:
    return subprocess.check_output (command)
  except OSError as e:
    raise RunError (' '.join (command), str (e))


#
# -----------------------------------------------------------------------------
#
# Runs a shell command and captures its output.
#
# This is a simplified shortcut to subprocess.check_output that raises RunError
# on failure. Command must be a string representing a shell command.
#

def shell_output (command):
  try:
    return subprocess.check_output (command, shell = True)
  except OSError as e:
    raise RunError (' '.join (command), str (e))


#
# -----------------------------------------------------------------------------
#
# Executes a pipeline of commands with each command running in its own process.
# If regex is not None, matching lines of the pipeline's output will be returned
# as a list. If file is not None, all output will be sent to the given file
# object using its write method and optionally sent to the console if
# g_args.log_to_console is also set.
#
# Note that commands is expected to be a list where each entry is also a list
# which is passed to subprocess.Popen to execute a command. If there is only
# one command in the list, then it is simply executed in a new process witout
# building up a pipeline.
#
# Raises Error if execution fails or terminates with a non-zero exit code.
#

def run_pipe (commands, regex = None, file = None):

  if not file:
    file = g_output_file

  recomp = re.compile (regex) if regex else None
  lines = []
  rc = 0

  # Note: obey log_to_console only if the console is not redirected to a file.
  # Also makes no sense to capture if file equals to sys.stdout (unless regex
  # is given). If file is None, we have to capture to hide any output at all.
  duplicate_output = g_args.log_to_console and g_log != sys.stdout and file != sys.stdout
  capture_output = duplicate_output or recomp or not file

  try:

    cmd = commands [0]

    if len (commands) == 1:

      if capture_output:
        proc = subprocess.Popen (cmd, stdout = subprocess.PIPE, stderr = subprocess.STDOUT, bufsize = 1)
        capture_file = proc.stdout
      else:
        proc = subprocess.Popen (cmd, stdout = file, stderr = subprocess.STDOUT, bufsize = 1)

    else:

      if capture_output:
        # Note: We can't use proc.stderr here as it's only a read end.
        rpipe, wpipe = os.pipe ()
        capture_file = os.fdopen (rpipe)
      else:
        wpipe = file

      proc = subprocess.Popen (cmd, stdout = subprocess.PIPE, stderr = wpipe)

      last_proc = proc
      for cmd in commands [1:]:
        last_proc = subprocess.Popen (cmd, stdin = last_proc.stdout,
                                      stdout = wpipe if cmd == commands [-1] else subprocess.PIPE,
                                      stderr = wpipe)

      if capture_output:
        os.close (wpipe)

    if capture_output:
      for line in iter (capture_file.readline, ''):
        if recomp:
          lines += recomp.findall (line)
        if duplicate_output:
          sys.stdout.write (line)
        if file:
          file.write (line)

    if len (commands) > 1:
      # TODO: we ignore the child exit code at the moment due to this bug:
      # http://trac.netlabs.org/rpm/ticket/267#ticket
      # Once it's fixed, we should report it to the caller. Note that we can
      # ignore it now only because CPIO_EXE luckily works per se, it just can't
      # close its end of the pipe gracefully (and e.g grep doesnt' work at all).
      rc = last_proc.wait ()

    rc = proc.wait ()
    msg = 'exit code %d' % rc

  except OSError as e:

    rc = 1
    msg = 'error %d (%s)' % (e.errno, e.strerror)

  finally:

    if rc:
      raise RunError (' '.join (cmd), msg)

  return lines


#
# -----------------------------------------------------------------------------
#
# Shortcut to #run_pipe for one command.
#

def run (command, regex = None):
  return run_pipe ([command], regex)


#
# -----------------------------------------------------------------------------
#
# Similar to #run_pipe but all output produced by and external commands will be
# be redirected to a log file.
#

def run_pipe_log (log_file, commands, regex = None):

  with open (log_file, 'w', buffering = 1) as f:

    start_ts = datetime.datetime.now ()
    f.write ('[%s, %s]\n' % (start_ts.strftime (DATETIME_FMT), ' | '.join (' '.join(c) for c in commands)))

    try:

      rc = 0
      msg = 'exit code 0'
      lines = run_pipe (commands, regex, f)

    except RunError as e:

      rc = 1
      cmd = e.cmd
      msg = e.msg

    finally:

      end_ts = datetime.datetime.now ()
      elapsed = str (end_ts - start_ts).rstrip ('0')
      f.write ('[%s, %s, %s s]\n' % (end_ts.strftime (DATETIME_FMT), msg, elapsed))

      if rc:
        raise Error ('The following command failed with: %s:\n'
                     '  %s'
                     % (msg, ' '.join (cmd)),
                     log_file = log_file)

  return lines


#
# -----------------------------------------------------------------------------
#
# Shortcut to #run_pipe_log for one command.
#

def run_log (log_file, command, regex = None):
  return run_pipe_log (log_file, [command], regex)


#
# -----------------------------------------------------------------------------
#
# Similar to #run_log but runs a Python function. All output produced by #log
# and external commands run via #run and #run_pipe within this function will be
# redirected to a log file.
#

def func_log (log_file, func):

  with open (log_file, 'w', buffering = 1) as f:

    start_ts = datetime.datetime.now ()
    f.write ('[%s, Python %s]\n' % (start_ts.strftime (DATETIME_FMT), str (func)))

    try:

      # Cause redirection of #log to the given file.
      global g_output_file
      g_output_file = f

      rc = func () or 0
      msg = 'return code %d' % rc

    except RunError as e:

      rc = 1
      f.write ('%s: %s\n' % (e.cmd, e.msg))

    except:

      rc = 1
      f.write ('Unexpected exception occured:\n%s' % traceback.format_exc ())

    finally:

      g_output_file = None

      if rc:
        msg = 'exception ' + sys.exc_type.__name__

      end_ts = datetime.datetime.now ()
      elapsed = str (end_ts - start_ts).rstrip ('0')
      f.write ('[%s, %s, %s s]\n' % (end_ts.strftime (DATETIME_FMT), msg, elapsed))

      if rc:
        raise RunError (str (func), msg, log_file = log_file)


#
# -----------------------------------------------------------------------------
#
# Searches for a spec file in the provided path or in spec_dirs if no path is
# provided in spec. Assumes the `.spec` extension if it is missing. If the spec
# file is found, this function will do the following:
#
# - Load `rpmbuild-bot2.ini` into config if it exists in a spec_dirs directory
#   containing the spec file (directly or through children).
# - Load `rpmbuild-bot2.ini` into config if it exists in the same directory
#   where the spec file is found.
# - Log the name of the found spec file.
# - Return a tuple with the full path to the spec file, spec base name (w/o
#   path or extension) and full path to the auxiliary source directory for this
#   spec.
#
# Otherwise, Error is raised and no INI files are loaded.
#

def resolve_spec (spec, spec_dirs, config):

  found = 0

  if os.path.splitext (spec) [1] != '.spec' :
    spec += '.spec'

  dir = os.path.dirname (spec)
  if dir:
    spec_base = os.path.splitext (os.path.basename (spec)) [0]
    full_spec = os.path.abspath (spec)
    if os.path.isfile (full_spec):
      found = 1
      full_spec_dir = os.path.dirname (full_spec)
      for dirs in spec_dirs:
        for d in dirs:
          if os.path.samefile (d, full_spec_dir) or \
             os.path.samefile (os.path.join (d, spec_base), full_spec_dir):
            found = 2
            break
        else:
          continue
        break
  else:
    spec_base = os.path.splitext (spec) [0]
    for dirs in spec_dirs:
      for d in dirs:
        full_spec = os.path.abspath (os.path.join (d, spec))
        if os.path.isfile (full_spec):
          found = 2
          break
        else:
          full_spec = os.path.abspath (os.path.join (d, spec_base, spec))
          if os.path.isfile (full_spec):
            found = 2
            break
      else:
        continue
      break

  # Load directory INI files
  if found == 2:
    config.read (os.path.join (dirs [0], SCRIPT_INI_FILE))
    if not os.path.samefile (d, dirs [0]):
      config.read (os.path.join (d, SCRIPT_INI_FILE))

  # Load spec INI file
  if found >= 1:
    config.read (os.path.join (os.path.dirname (full_spec), SCRIPT_INI_FILE))

  # Figure out the auxiliary source dir for this spec
  spec_aux_dir = os.path.dirname (full_spec)
  if (os.path.basename (spec_aux_dir) != spec_base):
    spec_aux_dir = os.path.join (spec_aux_dir, spec_base)

  if (found == 0):
    if dir:
      raise Error ('Cannot find `%s`' % spec)
    else:
      raise Error ('Cannot find `%s` in %s' % (spec, spec_dirs))

  log ('Spec file       : %s' % full_spec)
  log ('Spec source dir : %s' % spec_aux_dir)

  # Validate some mandatory config options.
  if not config.get ('general:archs'):
    raise Error ('config', 'No value for option `general:archs`');

  return (full_spec, spec_base, spec_aux_dir)


#
# -----------------------------------------------------------------------------
#
# Reads settings of a given repository group from a given config and returns a
# dictionary with the following keys:
#
# - base: base directory of the group;
# - repos: list of group's repositories;
# - repo.REPO (a value from repos): dictionary with the following keys:
#   - layout: repo layout's name;
#   - base: base directoy of the repo (with group's base prepended);
#   - rpm, srpm, zip, log: directories of respective parts as defined by repo's
#     layout (with repo's base prepended).
#
# Besides repo.REPO for each repository from the group's repository list, there
# is also a special key `repos.None` (where None is a None constant rather a
# string). This key contains the respective local build directories where
# rpmbuild puts RPMs.
#

def read_group_config (group, config):

  d = dict ()

  group_section = 'group.%s' % group
  d ['base'] = config.get (group_section, 'base')
  d ['repos'] = config.getwords (group_section, 'repositories')

  if len (d ['repos']) == 0:
    raise Error ('config', 'No repositories in group `%s`' % group)

  for repo in d ['repos']:

    rd = dict ()

    repo_section = 'repository.%s:%s' % (group, repo)
    rd ['layout'] = config.get (repo_section, 'layout')
    rd ['base'] = repo_base = os.path.join (d ['base'], config.get (repo_section, 'base'))

    layout_section = 'layout.%s' % rd ['layout']
    rd ['rpm'] = os.path.join (repo_base, config.get (layout_section, 'rpm'))
    rd ['srpm'] = os.path.join (repo_base, config.get (layout_section, 'srpm'))
    rd ['zip'] = os.path.join (repo_base, config.get (layout_section, 'zip'))
    rd ['log'] = os.path.join (repo_base, config.get (layout_section, 'log'))

    d ['repo.%s' % repo] = rd

  ld = dict ()
  ld ['base'] = g_rpm ['_topdir']
  ld ['rpm'] = g_rpm ['_rpmdir']
  ld ['srpm'] = g_rpm ['_srcrpmdir']
  ld ['zip'] = g_zip_dir
  ld ['log'] = os.path.join (g_log_dir, 'build')

  d ['repo.%s' % None] = ld

  return d


#
# -----------------------------------------------------------------------------
#
# Returns a resolved path of a given file in a given repo.
#
# None as repo means the local build. Otherwise, it's a repo name from the INI
# file and group_config must also be not None and represent this repo's group.
#

def resolve_path (name, arch, repo = None, group_config = None):

  if arch in ['srpm', 'zip']:
    path = os.path.join (group_config ['repo.%s' % repo] [arch], name)
  else:
    path = os.path.join (group_config ['repo.%s' % repo] ['rpm'], arch, name)

  return path


#
# -----------------------------------------------------------------------------
#
# Reads the build summary file of spec_base located in a given group and
# returns the following as a tuple:
#
# - Full version as was defined by the spec.
#
# - Name of the user who built the spec followed by `@` and the hostname (both
# are non-empty strings).
#
# - Dict containing resolved file names of all RPM files built from the spec.
# The dict has the following keys: 'srpm', 'zip' and one key per each built
# arch. The first two keys contain a single file name. Each of the arch keys
# contains a list of file names.
#
# Passing None as group will read the summary from the local build directory.
# Otherwise, it must be a repository group name from the INI file and config
# must also be not None. In this case a summary file of the corresponding
# repository will be accessed.
#
# This method performs integrity checking of the summary file (version string
# validity, existence of files, their timestamps etc.) and raises an Error on
# any failure.
#

def read_build_summary (spec_base, repo = None, group_config = None):

  log_base = os.path.join (group_config ['repo.%s' % repo] ['log'], spec_base)

  try:

    summary = os.path.join (log_base, 'summary')
    with open (summary, 'r') as f:

      try:

        ln = 1
        ver_full = f.readline ().strip ()
        if not re.match (r'^%s$' % VER_FULL_REGEX, ver_full):
          raise Error ('Invalid version specification: `%s`' % ver_full)

        ln = 2
        build_user, build_time = f.readline ().strip ().split ('|')
        if not re.match (r'^%s$' % BUILD_USER_REGEX, build_user):
          raise Error ('Invalid build user specification: `%s`' % build_user)
        build_time = float (build_time)

        d = dict ()

        for line in f:

          ln += 1
          arch, name, mtime, size = line.strip ().split ('|')
          mtime = float (mtime)
          size = int (size)
          path = resolve_path (name, arch, repo, group_config)

          if os.path.getmtime (path) != mtime:
            raise Error ('%s:%s' % (summary, ln), '\nRecorded mtime differs from actual for `%s`')
          if os.path.getsize (path) != size:
            raise Error ('%s:%s' % (summary, ln), '\nRecorded size differs from actual for `%s`')

          if arch in ['srpm', 'zip']:
            d [arch] = path
          else:
            if arch in d:
              d [arch].append (path)
            else:
              d [arch] = [path]

      except (IOError, OSError) as e:
        raise Error ('%s:%s:\n%s' % (summary, ln, str (e)))

      except ValueError:
        raise Error ('%s:%s' % (summary, ln), 'Invalid field type or number of fields')

    return ver_full, build_user, build_time, d

  except IOError as e:
    if e.errno == 2:
      raise Error ('No build summary for `%s` (%s)' % (spec_base, summary),
                   hint = 'Use `build` command to build the packages first.')
    else:
      raise Error ('Cannot read build summary for `%s`:\n%s' % (spec_base, str (e)))


#
# -----------------------------------------------------------------------------
#
# Prepare for build and test commands. This includes the following:
#
# - Download legacy runtime libraries for the given spec if spec legacy is
#   configured.
#
# TODO: Actually implement it.
#

def build_prepare (full_spec, spec_base):

  pass


#
# -----------------------------------------------------------------------------
#
# Build command.
#

def build_cmd ():

  for spec in g_args.SPEC.split (','):

    config = copy.deepcopy (g_config)
    full_spec, spec_base, spec_aux_dir = resolve_spec (spec, g_spec_dirs, config)

    archs = config.getwords ('general:archs')

    log ('Targets: ' + ', '.join (archs) + ', ZIP (%s), SRPM' % archs [0])

    log_base = os.path.join (g_log_dir, 'build', spec_base)

    summary = os.path.join (log_base, 'summary')
    if os.path.isfile (summary):
      with open (summary, 'r') as f:
        ver = f.readline ().strip ()
      if g_args.force_command:
        log_note ('Overwriting previous build of `%s` (%s) due to -f option.' % (spec_base, ver))
      else:
        raise Error ('Build summary for `%s` (%s) already exists (%s)' % (spec_base, ver, summary),
                     hint = 'Use -f option to overwrite this build with another one w/o uploading it.')

    remove_path (log_base)
    ensure_dir (log_base)

    # Generate RPMs for all architectures.

    noarch_only = True
    base_rpms = None
    arch_rpms = dict ()

    for arch in archs:

      log_file = os.path.join (log_base, '%s.log' % arch)
      log ('Creating RPMs for `%(arch)s` target (logging to %(log_file)s)...' % locals ())

      rpms = run_log (log_file, [RPMBUILD_EXE, '--target=%s' % arch, '-bb',
                                 '--define=_sourcedir %s' % spec_aux_dir, full_spec],
                      r'^Wrote: +(.+\.(?:%s|noarch)\.rpm)$' % arch)

      if len (rpms):
        arch_rpms [arch] = rpms
        # Save the base arch RPMs for later.
        if not base_rpms:
          base_rpms = rpms
        # Check for noarch only.
        for r in rpms:
          if r.endswith ('.%s.rpm' % arch):
            noarch_only = False
            break
        if noarch_only:
          log ('Skipping other targets because `%s` produced only `noarch` RPMs.' % arch)
          break
      else:
        raise Error ('Cannot find `.(%(arch)s|noarch).rpm` file names in `%(log_file)s`.' % locals ())

    # Generate SRPM.

    log_file = os.path.join (log_base, 'srpm.log')
    log ('Creating SRPM (logging to %s)...' % log_file)

    srpm = run_log (log_file, [RPMBUILD_EXE, '-bs',
                               '--define=_sourcedir %s' % spec_aux_dir, full_spec],
                    r'^Wrote: +(.+\.src\.rpm)$') [0]

    if not srpm:
      raise Error ('Cannot find `.src.rpm` file name in `%s`.' % log_file)

    # Find package version.

    srpm_base = os.path.basename (srpm)
    spec_ver = re.match (r'(%s)-(%s)\.src\.rpm' % (spec_base, VER_FULL_REGEX), srpm_base)
    if not spec_ver or spec_ver.lastindex != 2:
      raise Error ('Cannot deduce package version from `%s`' % srpm)

    srpm_name = spec_ver.group (1)
    ver_full = spec_ver.group (2)
    if srpm_name != spec_base:
      raise Error ('Package name in `%(srpm_base)s` does not match .spec name `%(spec_base)s`.\n'
                   'Either rename `%(spec_base)s.spec` to `%(srpm_name)s.spec` or set `Name:` tag to `%(spec_base)s`.'  % locals())

    # Generate ZIP.

    log_file = os.path.join (log_base, 'zip.log')
    log ('Creating ZIP (logging to %s)...' % log_file)

    zip_file = os.path.join (g_zip_dir, '%s-%s.zip' % (spec_base, ver_full.replace ('.', '_')))

    def gen_zip ():

      os.chdir (g_zip_dir)
      remove_path ('@unixroot')

      for r in base_rpms:
        log ('Unpacking `%s`...' % r)
        run_pipe ([[RPM2CPIO_EXE, r], [CPIO_EXE, '-idm']])

      remove_path (zip_file)
      log ('Creating `%s`...' % zip_file)
      run_pipe ([['zip', '-mry9', zip_file, '@unixroot']])

    func_log (log_file, gen_zip)

    # Write a summary with all generated packages for further reference.

    def file_data (path):
      return '%s|%s|%s' % (os.path.basename (path), os.path.getmtime (path), os.path.getsize (path))

    with open ('%s.tmp' % summary, 'w') as f:
      f.write (ver_full + '\n')
      f.write ('%s@%s|%s' % (g_username, g_hostname, time.time ()) + '\n')
      f.write ('srpm|%s\n' % file_data (srpm))
      f.write ('zip|%s\n' % file_data (zip_file))
      for a in arch_rpms.keys ():
        for r in arch_rpms [a]:
          f.write ('%s|%s\n' % (a, file_data (r)))

    # Everything succeeded.
    os.rename ('%s.tmp' % summary, summary)
    log ('Generated all packages for version %s.' % ver_full)


#
# -----------------------------------------------------------------------------
#
# Test command.
#

def test_cmd ():

  g_test_cmd_steps = {
    'all': ['-bb'],
    'prep': ['-bp', '--short-circuit'],
    'build': ['-bc', '--short-circuit'],
    'install': ['-bi', '--short-circuit'],
    'pack': ['-bb', '--short-circuit'],
  }

  opts = g_test_cmd_steps [g_args.STEP]

  for spec in g_args.SPEC.split (','):

    config = copy.deepcopy (g_config)
    full_spec, spec_base, spec_aux_dir = resolve_spec (spec, g_spec_dirs, config)

    if g_args.STEP == 'all':
      build_prepare (full_spec, spec_base)

    log_file = os.path.join (g_log_dir, 'test',
                             spec_base + ('' if g_args.STEP == 'all' else '.' + g_args.STEP) + '.log')

    rotate_log (log_file)

    base_arch = config.getwords ('general:archs') [0]

    log ('Creating test RPMs for `%(base_arch)s` target (logging to %(log_file)s)...' % locals ())

    rpms = run_log (log_file, [RPMBUILD_EXE, '--target=%s' % base_arch, '--define=dist %nil',
                               '--define=_sourcedir %s' % spec_aux_dir] + opts + [full_spec],
                    r'^Wrote: +(.+\.(?:%s|noarch)\.rpm)$' % base_arch)

    # Show the generated RPMs when appropriate.
    if g_args.STEP == 'all' or g_args.STEP == 'pack':
      if len (rpms):
        log ('Successfully generated the following RPMs:')
        log ('\n'.join (rpms))
      else:
        raise Error ('Cannot find `.(%(base_arch)s|noarch).rpm` file names in `%(log_file)s`.' % locals ())


#
# -----------------------------------------------------------------------------
#
# Move command. Also used to implement upload.
#

def move_cmd ():

  is_upload = g_args.COMMAND == 'upload'

  if not is_upload:
    # No need in per-spec INI loading
    raise Error ('Not implemented!')

  for spec in g_args.SPEC.split (','):

    if is_upload:
      config = copy.deepcopy (g_config)
      full_spec, spec_base, spec_aux_dir = resolve_spec (spec, g_spec_dirs, config)

    group, to_repo, from_repo = (g_args.GROUP.split (':', 2) + [None, None]) [:3]

    if is_upload:
      # Always build directory (from_repo should be None).
      if from_repo:
        raise Error ('Extra input in GROUP spec: `%s`' % from_repo)

    group_config = read_group_config (group, config)
    repos = group_config ['repos']

    if not is_upload and not from_repo:
      # Look for summary in one of the repos.
      for repo in repos:
        if os.path.isfile (os.path.join (group_config ['repo.%s' % repo] ['log'], spec_base, 'summary')):
          from_repo = repo

    if from_repo and not from_repo in group_config ['repos']:
      raise Error ('No repository `%s` in group `%s`' % (from_repo, group))

    if not to_repo:
      if from_repo:
        i = repos.index (from_repo)
        if i < len (repos):
          to_repo = repos [i + 1]
        else:
          raise Error ('No repository after `%s` in group `%s`' % (from_repo, group))
      else:
        to_repo = repos [0]

    if not to_repo in repos:
      raise Error ('No repository `%s` in group `%s`' % (to_repo, group))

    from_repo_config = group_config ['repo.%s' % from_repo]
    to_repo_config = group_config ['repo.%s' % to_repo]

    log ('From repository : %s' % from_repo_config ['base'])
    log ('To repository   : %s' % to_repo_config ['base'])

    if is_upload:
      ver_full, build_user, build_time, rpms = read_build_summary (spec_base, from_repo, group_config)
    else:
      raise Error ('Not implemented!')

    log ('Version         : %s' % ver_full)
    log ('Build user      : %s' % build_user)
    log ('Build time      : %s' % to_localtimestr (build_time))

    to_summary = os.path.join (to_repo_config ['log'], spec_base, ver_full, 'summary')
    if os.path.isfile (to_summary):
      if g_args.force_command:
        log_note ('Overwriting previous build of `%s` due to -f option.' % spec_base)
      else:
        raise Error ('Build summary for `%s` already exists (%s)' % (spec_base, to_summary),
                     hint = 'If recovering from a failure, use -f option to overwrite this build with a new one.')

    # Copy RPMs.

    rpms_to_copy = []

    for arch in rpms.keys ():
      if arch in ['srpm', 'zip']:
        src = rpms [arch]
        dst = to_repo_config [arch]
        rpms_to_copy.append ((src, dst))
      else:
        dst = os.path.join (to_repo_config ['rpm'], arch)
        for src in rpms [arch]:
          rpms_to_copy.append ((src, dst))

      for src, dst in rpms_to_copy:
        log ('Copying %s -> %s...' % (src, dst))
        if not os.path.isdir (dst):
          raise Error ('%s' % dst, 'Not a directory')
        shutil.copy2 (src, dst)

    # Copy build logs and summary.

    from_log = os.path.join (from_repo_config ['log'], spec_base)
    if not is_upload:
      from_log = os.path.join (from_log, ver_full)

    zip_path = os.path.join (from_log, 'logs.zip')

    if is_upload:
      # Local build - zip all logs (otherwise they are already zipped).
      log ('Packing logs to %s...' % zip_path)
      zip_files = []
      for arch in rpms.keys ():
        zip_files.append (os.path.join (from_log, '%s.log' % arch))
      run_pipe ([['zip', '-jy9', zip_path] + zip_files])

    to_log = os.path.join (to_repo_config ['log'], spec_base, ver_full)

    log ('Copying logs from %s -> %s...' % (from_log, to_log))

    remove_path (to_log)
    ensure_dir (to_log)

    logs_to_copy = [zip_path, os.path.join (from_log, 'summary')]
    for src in logs_to_copy:
      shutil.copy2 (src, to_log)

    log ('Removing copied packages...')

    for src, _ in rpms_to_copy:
      os.remove (src)

    if not is_upload:

      # Clean up remote repository.
      log ('Removing copied logs...')
      for src in logs_to_copy:
        os.remove (src)

    else:

      # Archive local logs.
      archive_dir = os.path.join (g_log_dir, 'archive', spec_base, ver_full)
      log ('Archiving logs to %s...' % archive_dir)
      remove_path (archive_dir)
      ensure_dir (archive_dir)
      for src in logs_to_copy:
        shutil.move (src, archive_dir)

      # Remove unpacked logs.
      for src in zip_files:
        os.remove (src)

    # Remove source log dir.
    remove_path (from_log)


#
# =============================================================================
#
# Main
#

# Script's start timestamp.
g_start_ts = datetime.datetime.now ()

# Script's own log file.
g_log = None

# Cache of RPM macro values.
g_rpm = {}

# RPM macros to pre-evaluate.
g_rpmbuild_used_macros = ['_topdir', '_sourcedir', 'dist', '_bindir', '_rpmdir', '_srcrpmdir']

# Script's output redirection (for #func_log).
g_output_file = None

# Parse command line.

g_cmdline = argparse.ArgumentParser (formatter_class = argparse.ArgumentDefaultsHelpFormatter, description = '''
A frontend to rpmbuild that provides a centralized way to build RPM packages from
RPM spec files and move them later across configured repositories.''', epilog = '''
Specify COMMAND -h to get help on a particular command.''')

g_cmdline.add_argument ('--version', action = 'version', version = '%(prog)s ' + VERSION)
g_cmdline.add_argument ('-l', action = 'store_true', dest = 'log_to_console', help = 'echo log output to console')
g_cmdline.add_argument ('-f', action = 'store_true', dest = 'force_command', help = 'force command execution')

g_cmds = g_cmdline.add_subparsers (dest = 'COMMAND', metavar = 'COMMAND', help = 'command to run:')

# Parse data for test command.

g_cmd_test = g_cmds.add_parser ('test',
  help = 'do test build (one arch)', description = '''
Runs a test build of SPEC for one architecture. STEP may speficty a rpmbuild
shortcut to go to a specific build step.''',
  formatter_class = g_cmdline.formatter_class)

g_cmd_test.add_argument ('STEP', nargs = '?', choices = ['all', 'prep', 'build', 'install', 'pack'], default = 'all', help = 'build step: %(choices)s', metavar = 'STEP')
g_cmd_test.add_argument ('SPEC', help = 'spec file (comma-separated if more than one)')
g_cmd_test.set_defaults (cmd = test_cmd)

# Parse data for build command.

g_cmd_build = g_cmds.add_parser ('build',
  help = 'do normal build (all configured archs)', description = '''
Builds SPEC for all configured architectures. If SPEC does not have a path (recommended),
it will be searcherd in configured SPEC directories.''',
  formatter_class = g_cmdline.formatter_class)

g_cmd_build.add_argument ('SPEC', help = 'spec file (comma-separated if more than one)')
g_cmd_build.set_defaults (cmd = build_cmd)

# Parse data for upload command.

g_cmd_upload = g_cmds.add_parser ('upload',
  help = 'uupload build results to repository group', description = '''
Uploads all RPMs generated from SPEC to a repository of a configured repository group.
If REPO is not specified, the first GROUP's repository is used.''',
  formatter_class = g_cmdline.formatter_class)

g_cmd_upload.add_argument ('GROUP', help = 'repository group and optional repository name from INI file', metavar = 'GROUP[:REPO]')
g_cmd_upload.add_argument ('SPEC', help = 'spec file (comma-separated if more than one)')
g_cmd_upload.set_defaults (cmd = move_cmd)

# Finally, do the parsing.

g_args = g_cmdline.parse_args ()

g_main_ini_path = os.path.expanduser ('~/rpmbuild-bot2.ini')

g_config = Config (g_rpm)

g_spec_dirs = []

try:

  # Detect user and hostname.

  g_username = getpass.getuser ()
  if not g_username:
    raise Error ('Cannot determine user name of this build machine.')

  g_hostname = socket.gethostname ()
  if not g_hostname:
    raise Error ('Cannot determine host name of this build machine.')

  # Read the main config file.

  try:
    with open (g_main_ini_path, 'r') as f:
      g_config.readfp (f)
  except (IOError, OSError) as e:
    raise Error ('Cannot read configuration from `%s`:\n%s' % (g_main_ini_path, str (e)))

  for d in g_config.getlines ('general:spec_dirs'):
    if d [0] == '+' and len (g_spec_dirs):
      g_spec_dirs [-1].append (d [1:].lstrip ())
    else:
      g_spec_dirs.append ([d])

  rc = 0

  # Pre-evaluate some RPMBUILD macros (this will also chedk for RPMBUILD_EXE availability).

  for i, m in enumerate (command_output ([
    RPMBUILD_EXE, '--eval',
    ''.join ('|%{?' + s + '}' for s in g_rpmbuild_used_macros).lstrip ('|')
  ]).strip ().split ('|')):
    g_rpm [g_rpmbuild_used_macros [i]] = m

  for m in ['_topdir', '_sourcedir']:
    if not g_rpm [m] or not os.path.isdir (g_rpm [m]):
      raise Error ('Value of `%%%s` in rpmbuild is `%s` and not a directory' % (m, g_rpm [m]))

  # Prepare some (non-rpmbuild-standard) directories.

  g_zip_dir = os.path.join (g_rpm ['_topdir'], 'zip')
  g_log_dir = os.path.join (g_rpm ['_topdir'], 'logs')

  for d in [g_zip_dir] + [os.path.join (g_log_dir, f) for f in ('test', 'build')]:
    ensure_dir (d)

  # Create own log file unless redirected to a file.

  if sys.stdout.isatty():
    d = os.path.join (g_log_dir, SCRIPT_LOG_FILE)
    rotate_log (d)
    g_log = open (d, 'w', buffering = 1)
  else:
    g_log = sys.stdout

  g_log.write ('[%s, %s]\n' % (g_start_ts.strftime (DATETIME_FMT), ' '.join (sys.argv)))

  # Run command.

  g_args.cmd ()

except (ConfigParser.NoSectionError, ConfigParser.NoOptionError, ConfigParser.InterpolationError) as e:

  log_err ('config', str (e))
  log_hint ('Check `%s` or spec-specific INI files' % g_main_ini_path)
  rc = 1

except (IOError, OSError) as e:
  log_err (str (e))
  rc = 2

except RunError as e:

  msg = 'The following command failed with: %s:\n  %s' % (e.msg, e.cmd)
  if e.log_file:
    msg += '\nInspect `%s` for more info.' % e.log_file
  log_err (e.prefix, msg)
  if e.hint:
    log_hint (e.hint)
  rc = e.code

except Error as e:

  log_err (e.prefix, e.msg)
  if e.hint:
    log_hint (e.hint)
  rc = e.code

except:

  log_err ('Unexpected exception occured:')
  log (traceback.format_exc ())
  rc = 127

finally:

  end_ts = datetime.datetime.now ()
  elapsed = str (end_ts - g_start_ts).rstrip ('0')

  if g_log != sys.stdout:
    sys.stdout.write ('%s (%s s).\n' % (rc and 'Failed with exit code %s' % rc or 'Succeeded', elapsed))

  # Finalize own log file.
  if g_log:
    g_log.write ('[%s, exit code %d, %s s]\n\n' % (end_ts.strftime (DATETIME_FMT), rc, elapsed))
    g_log.close ()

exit (rc)
