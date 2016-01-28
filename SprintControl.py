
# This is the Sprint PythonControl interface implementation.
# For reference, in Sprint code, see:
#  * src/Nn/PythonControl.cc
#  * src/Tools/NnTrainer/python_control_demo.py
# This interface will behave similar as SprintExternInterface.
# See SprintErrorSignals for the other end.
# It can also be used as a PythonSegmentOrdering interface.
# It also supports SprintNnPythonLayer.

print("CRNN SprintControl Python module load")

import rnn
import Debug
import os
import numpy
from TaskSystem import Pickler, Unpickler
from threading import RLock, Condition



rnn.initBetterExchook()
Debug.initFaulthandler(sigusr1_chain=True)  # Sprint also handles SIGUSR1.
rnn.initThreadJoinHack()

# Start Sprint PythonControl interface. {

def init(name, reference, config, **kwargs):
  print("CRNN SprintControl init: %r, %r, %r, %r" % (name, reference, config, kwargs))

  config = config.split(",")
  config = {key: value for (key, value) in [s.split(":", 1) for s in config if s]}

  # Remaining Sprint interface is in this PythonControl instance.
  return PythonControl.create(c2p_fd=int(config["c2p_fd"]), p2c_fd=int(config["p2c_fd"]))

# End Sprint PythonControl interface. }

# Start Sprint PythonSegmentOrder interface. {

def getSegmentList(corpusName, segmentList, config, **kwargs):
  print("CRNN SprintControl getSegmentList: %r, %r" % (corpusName, config))

  # If we were not initialized via PythonControl interface, this will initialize us
  # and setup the communication channel (PythonControl).
  init(name="CRNN.PythonSegmentOrder", reference=corpusName, config=config)
  PythonControl.instance.check_control_loop_running()
  for segment_name in PythonControl.instance.segment_list_iterator():
    yield segment_name

# End Sprint PythonSegmentOrder interface. }

# Start SprintNnPythonLayer. {

class SprintNnPythonLayer:

  def __init__(self, config, **kwargs):
    print("SprintNnPythonLayer.__init__: %r, %r" % (config, kwargs))
    # If we were not initialized via PythonControl interface, this will initialize us
    # and setup the communication channel (PythonControl).
    init(name="CRNN.SprintNnPythonLayer", reference=self, config=config)
    self.input_size = None
    self.output_size = None

  def setInputDimension(self, stream, size):
    print("SprintNnPythonLayer.setInputDimension: %r, %r" % (stream, size))
    assert stream == 0, "we only support a single input stream (for now)"
    self.input_size = size

  def setOutputDimension(self, size):
    print("SprintNnPythonLayer.setOutputDimension: %r" % size)
    self.output_size = size

  def initializeNetworkParameters(self):
    print("SprintNnPythonLayer.initializeNetworkParameters")
    # Just ignore.

  def loadNetworkParameters(self, filename):
    print("SprintNnPythonLayer.loadNetworkParameters: %r" % filename)
    # Just ignore.

  def saveNetworkParameters(self, filename):
    print("SprintNnPythonLayer.saveNetworkParameters: %r" % filename)
    # Just ignore.

  def isTrainable(self):
    # Always trainable.
    return True

  def getNumberOfFreeParameters(self):
    # For now, just ignore. Not important.
    return 0

  def forward(self, input):
    """
    :param input: tuple of input matrices of format (input_size,time). we ignore them.
    :return: single output matrix of format (output_size,time)
    """
    print("SprintNnPythonLayer.forward: %s" % input[0].shape if input else repr(input)[:10])
    assert len(input) == 1
    assert input[0].ndim == 2
    assert input[0].shape[0] == self.input_size
    seg_len = input[0].shape[1]
    posteriors = PythonControl.instance.get_current_seg_posteriors(seg_len=seg_len)  # (time,label)
    assert posteriors.shape == (seg_len, self.output_size)
    return posteriors.T

  def backpropagate(self, errorSignalIn):
    """
    :param errorSignalIn: matrix of format (output_size,time)
    :return: tuple of matrices of format (input_size,time)
    """
    print("SprintNnPythonLayer.backpropagate: %r" % errorSignalIn.shape)
    assert errorSignalIn.ndim == 2
    assert errorSignalIn.shape[0] == self.output_size
    seg_len = errorSignalIn.shape[1]
    PythonControl.instance.set_current_seg_error_signal(seg_len=seg_len, error_signal=errorSignalIn.T)
    # must return a 1-tuple
    return (numpy.zeros((self.input_size, seg_len), dtype="float32"),)

# End SprintNnPythonLayer. }


class PythonControl:

  """
  This will send data to CRNN over a pipe.
  We expect that we are child process and the parent process has spawned us,

  An instance of this class is also the interface for multiple Sprint interfaces, i.e.:
    * PythonControl (standalone via NnTrainer tool)
    * PythonControl (via SegmentwiseNnTrainer)
    * implicitly PythonSegmentOrder (see code above)
  """

  Version = 1  # increase when some protocol changes
  instance = None; ":type: PythonControl"

  @classmethod
  def create(cls, **kwargs):
    print "PythonControl init", kwargs
    if cls.instance: return cls.instance
    return PythonControl(**kwargs)

  def __init__(self, c2p_fd, p2c_fd, **kwargs):
    """
    :param int c2p_fd: child-to-parent file descriptor
    :param int p2c_fd: parent-to-child file descriptor
    """
    assert not self.__class__.instance, "only one instance expected"
    self.__class__.instance = self
    self.cond = Condition()
    self.pipe_c2p = os.fdopen(c2p_fd, "w")
    self.pipe_p2c = os.fdopen(p2c_fd, "r")
    self.callback = None
    self.control_loop_started = False
    self.control_loop_exited = False
    self.have_new_seg = False
    self.have_new_error_signal = False
    self.seg_name = None
    self.seg_len = None
    self.posteriors = None
    self.asked_for_posteriors = False
    self.notified_for_segment = False
    self.error_signal = None
    self.loss = None

  def _send(self, dataType, args=None):
    Pickler(self.pipe_c2p).dump((dataType, args))
    self.pipe_c2p.flush()

  def _read(self):
    return Unpickler(self.pipe_p2c).load()

  def close(self):
    self.pipe_c2p.close()
    self.pipe_p2c.close()

  def handle_cmd_exit(self):
    self.close()
    raise SystemExit

  def handle_cmd_init(self, name, version):
    assert version == self.Version
    return "SprintControl", self.Version

  def handle_get_loss_and_error_signal(self, seg_name, seg_len, posteriors_str):
    posteriors = numpy.fromstring(posteriors_str, dtype="float32")
    with self.cond:
      self.have_new_seg = True
      self.have_new_error_signal = False
      self.seg_name = seg_name
      self.seg_len = seg_len
      self.posteriors = posteriors
      self.error_signal = None
      self.loss = None
      self.asked_for_posteriors = False
      self.notified_for_segment = False
      self.cond.notifyAll()
    loss, error_signal = self.callback("get_loss_and_error_signal", seg_name, seg_len, posteriors)
    with self.cond:
      self.have_new_error_signal = True
      self.cond.notifyAll()
    error_signal_str = posteriors.astype('float32').tostring()
    return loss, error_signal_str

  def handle_cmd(self, cmd, *args):
    func = getattr(self, "handle_cmd_%s" % cmd)
    return func(*args)

  def handle_next(self):
    args = self._read()
    try:
      if not isinstance(args, tuple): raise TypeError("expected tuple but got %r" % args)
      if len(args) < 1: raise Exception("need multiple args (cmd, ...)")
      res = self.handle_cmd(*args)
    except Exception as e:
      self._send(("exception", str(e)))
    else:
      assert isinstance(res, tuple)
      self._send(("ok",) + res)

  # Called by Sprint.
  def run_control_loop(self, callback, **kwargs):
    print("CRNN PythonControl run_control_loop: %r, %r" % (callback, kwargs))
    print(">> Version: %r" % callback("version"))
    self.callback = callback
    with self.cond:
      assert not self.control_loop_started
      self.control_loop_started = True
      self.cond.notifyAll()
    while True:
      self.handle_next()
    with self.cond:
      self.control_loop_exited = False
      self.cond.notifyAll()

  # Called by Sprint.
  def exit(self, **kwargs):
    print("CRNN PythonControl exit: %r" % kwargs)

  def check_control_loop_running(self):
    if self.control_loop_started: return
    self.run_threaded_control_loop()

  def run_threaded_control_loop(self):
    from threading import Thread
    def control_loop():
      self.run_control_loop(self.own_callback)
    t = Thread(target=control_loop, name="SprintControl.PythonControl.threaded_control_loop")
    t.daemon = True
    t.start()
    while True:
      with self.cond:
        if self.control_loop_started: return
        assert t.isAlive()
        self.cond.wait(timeout=1)

  def own_callback(self, cmd, *args):
    """
    This is used if we run our own control loop via run_threaded_control_loop.
    """
    func = getattr(self, "own_cb_%s" % cmd)
    return func(*args)

  def own_cb_version(self):
    return "<version>CRNN.own_callback</version>"

  def own_cb_get_loss_and_error_signal(self, seg_name, seg_len, posteriors):
    # Wait until we get the loss and error signal.
    while True:
      with self.cond:
        if self.loss is not None and self.error_signal is not None:
          return self.loss, self.error_signal
        self.cond.wait(timeout=1)

  # Called by Sprint.
  def init_segment(self, segment_name):
    print "CRNN.SprintControl init_segment", segment_name
    with self.cond:
      assert self.seg_name == segment_name
      self.notified_for_segment = True

  # Called by Sprint.
  def notify_segment_loss(self, segment_name, loss):
    print "CRNN.SprintControl notify_segment_loss", segment_name, loss
    self.set_current_seg_loss(seg_name=segment_name, loss=loss)

  def get_current_seg_posteriors(self, seg_len):
    """
    :param int seg_len: just for double checking, the length of the current segment
    :return: matrix (time,label)
    """
    with self.cond:
      assert self.seg_len == seg_len
      assert self.posteriors.shape[0] == seg_len
      self.asked_for_posteriors = True
      return self.posteriors

  def set_current_seg_error_signal(self, seg_len, error_signal):
    """
    :param int seg_len: just for double checking, the length of the current segment
    :param error_signal: matrix (time,label)
    """
    with self.cond:
      assert self.seg_len == seg_len
      assert error_signal.ndim == 2
      assert error_signal.shape[0] == seg_len
      self.error_signal = error_signal
      self.cond.notifyAll()

  def set_current_seg_loss(self, seg_name, loss):
    """
    :param str seg_name: just for double checking, the name of the current segment
    :param float loss: the loss of the current seg
    """
    with self.cond:
      assert self.seg_name == seg_name
      self.loss = loss
      self.cond.notifyAll()

  def skip_segment_loss_and_error(self):
    with self.cond:
      assert self.posteriors is not None
      if self.loss is None:
        self.loss = float("inf")
      if self.error_signal is None:
        self.error_signal = numpy.zeros_like(self.posteriors)
      self.cond.notifyAll()

  def segment_list_iterator(self):
    with self.cond:
      assert self.control_loop_started

    while True:  # outer loop
      # wait until we get new segment
      while True:
        with self.cond:
          if self.have_new_seg:
            self.have_new_seg = False
            break
          if self.control_loop_exited:
            return  # no more segments
          self.cond.wait(timeout=1)

      # We got a new segment name from the parent CRNN process (via self.handle_get_loss_and_error_signal()).
      # We wait in this segment because we wait to get the error signal from Sprint (via SprintNnPythonLayer.backpropagate()).
      # Sprint waits currently for us to get the new segment (in the PythonSegmentOrder code).
      # Once it gets it, it will call SprintNnPythonLayer.forward(), then calculate the loss and error signal
      # and then call SprintNnPythonLayer.backpropagate().
      assert self.seg_name
      yield self.seg_name

      # When we are back here, Sprint asks for the next segment.
      # It means that is has finished any processing with this segment.
      with self.cond:
        # See self.handle_get_loss_and_error_signal().
        # We are still stuck in there in the other thread, if not self.have_new_error_signal.
        # Maybe the PythonLayer was not used?
        # Or Sprint could not calculate the criterion for this segment (bad lattice or so).
        if not self.have_new_error_signal:
          print "CRNN SprintControl skip segment:", self.seg_name
          if not self.notified_for_segment:
            print "Do you use PythonControl in the Sprint trainer?"
          if not self.asked_for_posteriors:
            print "Do you use PythonLayer in Sprint?"
          self.skip_segment_loss_and_error()