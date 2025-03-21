# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import contextmanager
from functools import wraps
import glob
import gzip
import http.server
import json
import os
import socketserver
import threading
import warnings

from typing import Callable, Optional

from absl import logging
from jax._src import traceback_util
traceback_util.register_exclusion(__file__)

from jax._src.lib import xla_bridge
from jax._src.lib import xla_client

_profiler_server: Optional[xla_client.profiler.ProfilerServer] = None


def start_server(port: int):
  """Starts the profiler server on port `port`.

  Using the "TensorFlow profiler" feature in `TensorBoard
  <https://www.tensorflow.org/tensorboard>`_ 2.2 or newer, you can
  connect to the profiler server and sample execution traces that show CPU,
  GPU, and/or TPU device activity.
  """
  global _profiler_server
  if _profiler_server is not None:
    raise ValueError("Only one profiler server can be active at a time.")
  _profiler_server = xla_client.profiler.start_server(port)
  return _profiler_server


def stop_server():
  """Stops the running profiler server."""
  global _profiler_server
  if _profiler_server is None:
    raise ValueError("No active profiler server.")
  _profiler_server = None # Should destroy the profiler server


class _ProfileState:
  def __init__(self):
    self.profile_session = None
    self.log_dir = None
    self.create_perfetto_link = False
    self.lock = threading.Lock()

_profile_state = _ProfileState()


def start_trace(log_dir, create_perfetto_link: bool = False):
  """Starts a profiler trace.

  The trace will capture CPU, GPU, and/or TPU activity, including Python
  functions and JAX on-device operations. Use ``stop_trace()`` to end the trace
  and save the results to ``log_dir``.

  The resulting trace can be viewed with TensorBoard. Note that TensorBoard
  doesn't need to be running when collecting the trace.

  Only once trace may be collected a time. A RuntimeError will be raised if
  ``start_trace()`` is called while another trace is running.

  Args:
    log_dir: The directory to save the profiler trace to (usually the
      TensorBoard log directory).
    create_perfetto_link: A boolean which, if true, creates and prints link to
      the Perfetto trace viewer UI (https://ui.perfetto.dev). The program will
      block until the link is opened and Perfetto loads the trace.
  """
  with _profile_state.lock:
    if _profile_state.profile_session is not None:
      raise RuntimeError("Profile has already been started. "
                         "Only one profile may be run at a time.")
    _profile_state.profile_session = xla_client.profiler.ProfilerSession()
    _profile_state.create_perfetto_link = create_perfetto_link
    _profile_state.log_dir = log_dir

def _write_perfetto_trace_file(log_dir):
  # Navigate to folder with the latest trace dump to find `trace.json.jz`
  curr_path = os.path.abspath(log_dir)
  root_trace_folder = os.path.join(curr_path, "plugins", "profile")
  trace_folders = [os.path.join(root_trace_folder, trace_folder) for
      trace_folder in os.listdir(root_trace_folder)]
  latest_folder = max(trace_folders, key=os.path.getmtime)
  trace_jsons = glob.glob(os.path.join(latest_folder, "*.trace.json.gz"))
  if len(trace_jsons) != 1:
    raise ValueError(f"Invalid trace folder: {latest_folder}")
  trace_json, = trace_jsons

  logging.info("Loading trace.json.gz and removing its metadata...")
  # Perfetto doesn't like the `metadata` field in `trace.json` so we remove
  # it.
  # TODO(sharadmv): speed this up by updating the generated `trace.json`
  # to not include metadata if possible.
  with gzip.open(trace_json, "rb") as fp:
    trace = json.load(fp)
    del trace["metadata"]
  filename = "perfetto_trace.json.gz"
  perfetto_trace = os.path.join(latest_folder, filename)
  logging.info("Writing perfetto_trace.json.gz...")
  with gzip.open(perfetto_trace, "w") as fp:
    fp.write(json.dumps(trace).encode("utf-8"))
  return perfetto_trace

class _PerfettoServer(http.server.SimpleHTTPRequestHandler):
  """Handles requests from `ui.perfetto.dev` for the `trace.json`"""

  def end_headers(self):
    self.send_header('Access-Control-Allow-Origin', '*')
    return super().end_headers()

  def do_GET(self):
    self.server.last_request = self.path
    return super().do_GET()

  def do_POST(self):
    self.send_error(404, "File not found")

def _host_perfetto_trace_file(log_dir):
  # ui.perfetto.dev looks for files hosted on `127.0.0.1:9001`. We set up a
  # TCP server that is hosting the `perfetto_trace.json.gz` file.
  port = 9001
  abs_filename = _write_perfetto_trace_file(log_dir)
  orig_directory = os.path.abspath(os.getcwd())
  directory, filename = os.path.split(abs_filename)
  try:
    os.chdir(directory)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(('127.0.0.1', port), _PerfettoServer) as httpd:
      url = f"https://ui.perfetto.dev/#!/?url=http://127.0.0.1:{port}/{filename}'"
      print(f"Open URL in browser: {url}")

      # Once ui.perfetto.dev acquires trace.json from this server we can close
      # it down.
      while httpd.__dict__.get('last_request') != '/' + filename:
        httpd.handle_request()
  finally:
    os.chdir(orig_directory)

def stop_trace():
  """Stops the currently-running profiler trace.

  The trace will be saved to the ``log_dir`` passed to the corresponding
  ``start_trace()`` call. Raises a RuntimeError if a trace hasn't been started.
  """
  with _profile_state.lock:
    if _profile_state.profile_session is None:
      raise RuntimeError("No profile started")
    _profile_state.profile_session.stop_and_export(_profile_state.log_dir)
    if _profile_state.create_perfetto_link:
      _host_perfetto_trace_file(_profile_state.log_dir)
    _profile_state.profile_session = None
    _profile_state.create_perfetto_link = False
    _profile_state.log_dir = None


@contextmanager
def trace(log_dir, create_perfetto_link=False):
  """Context manager to take a profiler trace.

  The trace will capture CPU, GPU, and/or TPU activity, including Python
  functions and JAX on-device operations.

  The resulting trace can be viewed with TensorBoard. Note that TensorBoard
  doesn't need to be running when collecting the trace.

  Only once trace may be collected a time. A RuntimeError will be raised if a
  trace is started while another trace is running.

  Args:
    log_dir: The directory to save the profiler trace to (usually the
      TensorBoard log directory).
    create_perfetto_link: A boolean which, if true, creates and prints link to
      the Perfetto trace viewer UI (https://ui.perfetto.dev). The program will
      block until the link is opened and Perfetto loads the trace.
  """
  start_trace(log_dir, create_perfetto_link)
  try:
    yield
  finally:
    stop_trace()


class TraceAnnotation(xla_client.profiler.TraceMe):
  """Context manager that generates a trace event in the profiler.

  The trace event spans the duration of the code enclosed by the context.

  For example:

  >>> x = jnp.ones((1000, 1000))
  >>> with jax.profiler.TraceAnnotation("my_label"):
  ...   result = jnp.dot(x, x.T).block_until_ready()

  This will cause a "my_label" event to show up on the trace timeline if the
  event occurs while the process is being traced.
  """
  pass


# TODO: remove this sometime after jax 0.2.11 is released
class TraceContext(TraceAnnotation):
  def __init__(self, *args, **kwargs):
    warnings.warn(
        "TraceContext has been renamed to TraceAnnotation. This alias "
        "will eventually be removed; please update your code.")
    super().__init__(*args, **kwargs)


class StepTraceAnnotation(TraceAnnotation):
  """Context manager that generates a step trace event in the profiler.

  The step trace event spans the duration of the code enclosed by the context.
  The profiler will provide the performance analysis for each step trace event.

  For example, it can be used to mark training steps and enable the profiler to
  provide the performance analysis per step:

  >>> while global_step < NUM_STEPS:                                           # doctest: +SKIP
  ...   with jax.profiler.StepTraceAnnotation("train", step_num=global_step):  # doctest: +SKIP
  ...     train_step()                                                         # doctest: +SKIP
  ...     global_step += 1                                                     # doctest: +SKIP

  This will cause a "train xx" event to show up on the trace timeline if the
  event occurs while the process is being traced by TensorBoard. In addition,
  if using accelerators, the device trace timeline will also show a "train xx"
  event. Note that "step_num" can be set as a keyword argument to pass the
  global step number to the profiler.

  """

  def __init__(self, name: str, **kwargs):
    super().__init__(name, _r=1, **kwargs)


# TODO: remove this sometime after jax 0.2.11 is released
class StepTraceContext(StepTraceAnnotation):
  def __init__(self, *args, **kwargs):
    warnings.warn(
        "StepTraceContext has been renamed to StepTraceAnnotation. This alias "
        "will eventually be removed; please update your code.")
    super().__init__(*args, **kwargs)


def annotate_function(func: Callable, name: Optional[str] = None,
                      **decorator_kwargs):
  """Decorator that generates a trace event for the execution of a function.

  For example:

  >>> @jax.profiler.annotate_function
  ... def f(x):
  ...   return jnp.dot(x, x.T).block_until_ready()
  >>>
  >>> result = f(jnp.ones((1000, 1000)))

  This will cause an "f" event to show up on the trace timeline if the
  function execution occurs while the process is being traced by TensorBoard.

  Arguments can be passed to the decorator via :py:func:`functools.partial`.

  >>> from functools import partial

  >>> @partial(jax.profiler.annotate_function, name="event_name")
  ... def f(x):
  ...   return jnp.dot(x, x.T).block_until_ready()

  >>> result = f(jnp.ones((1000, 1000)))
  """

  name = name or getattr(func, '__qualname__', None)
  name = name or func.__name__
  @wraps(func)
  def wrapper(*args, **kwargs):
    with TraceAnnotation(name, **decorator_kwargs):
      return func(*args, **kwargs)
    return wrapper
  return wrapper


# TODO: remove this sometime after jax 0.2.11 is released
def trace_function(*args, **kwargs):
  warnings.warn(
      "trace_function has been renamed to annotate_function. This alias "
      "will eventually be removed; please update your code.")
  return annotate_function(*args, **kwargs)



def device_memory_profile(backend: Optional[str] = None) -> bytes:
  """Captures a JAX device memory profile as ``pprof``-format protocol buffer.

  A device memory profile is a snapshot of the state of memory, that describes the JAX
  :class:`jax.DeviceArray` and executable objects present in memory and their
  allocation sites.

  For more information how to use the device memory profiler, see
  :doc:`/device_memory_profiling`.

  The profiling system works by instrumenting JAX on-device allocations,
  capturing a Python stack trace for each allocation. The instrumentation is
  always enabled; :func:`device_memory_profile` provides an API to capture it.

  The output of :func:`device_memory_profile` is a binary protocol buffer that
  can be interpreted and visualized by the `pprof tool
  <https://github.com/google/pprof>`_.

  Args:
    backend: optional; the name of the JAX backend for which the device memory
      profile should be collected.

  Returns:
    A byte string containing a binary `pprof`-format protocol buffer.
  """
  return xla_client.heap_profile(xla_bridge.get_backend(backend))


def save_device_memory_profile(filename, backend: Optional[str] = None):
  """Collects a device memory profile and writes it to a file.

  :func:`save_device_memory_profile` is a convenience wrapper around :func:`device_memory_profile`
  that saves its output to a ``filename``. See the
  :func:`device_memory_profile` documentation for more information.

  Args:
    filename: the filename to which the profile should be written.
    backend: optional; the name of the JAX backend for which the device memory
      profile should be collected.
  """
  profile = device_memory_profile(backend)
  with open(filename, "wb") as f:
    f.write(profile)
