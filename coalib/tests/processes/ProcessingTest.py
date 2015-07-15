import os
import queue
import unittest
import sys
import multiprocessing
import platform
import re
import subprocess

sys.path.insert(0, ".")
from coalib.results.HiddenResult import HiddenResult
from coalib.settings.ConfigurationGathering import gather_configuration
from coalib.output.printers.LogPrinter import LogPrinter
from coalib.processes.Processing import execute_section
from coalib.output.printers.ConsolePrinter import ConsolePrinter
from coalib.processes.CONTROL_ELEMENT import CONTROL_ELEMENT
from coalib.processes.Processing import process_queues, create_process_group
from coalib.misc.StringConstants import StringConstants
from coalib.settings.Section import Section


process_group_test_code = """
import time, subprocess, os, platform;
from coalib.misc.StringConstants import StringConstants;
p=subprocess.Popen([StringConstants.python_executable,
                  "-c",
                  "import time; time.sleep(0.1)"]);
pgid = p.pid if platform.system() == "Windows" else os.getpgid(p.pid);
print(p.pid, pgid)
p.terminate()
"""


class DummyProcess(multiprocessing.Process):
    def __init__(self, control_queue):
        multiprocessing.Process.__init__(self)
        self.control_queue = control_queue

    def is_alive(self):
        return not self.control_queue.empty()


class ProcessingTestInteractor(LogPrinter):
    def __init__(self, log_printer, result_queue, log_queue):
        LogPrinter.__init__(self)
        self.result_queue = result_queue
        self.log_queue = log_queue
        self.set_up = False

    def log_message(self, log_message, timestamp=None, **kwargs):
        self.log_queue.put(log_message)

    def print_results(self, section, result_list, file_dict):
        self.result_queue.put(result_list)


class MessageQueueingInteractor():
    """
    A simple interactor that pushes all results it gets to a queue for
    testing purposes.
    """

    def __init__(self):
        self.queue = queue.Queue()

    def print_results(self, section, result_list, file_dict):
        self.queue.put((result_list, file_dict))

    def get(self):
        return self.queue.get(timeout=0)


class ProcessingTest(unittest.TestCase):
    def setUp(self):
        config_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__),
            "section_executor_test_files",
            ".coafile"))
        self.testcode_c_path = os.path.join(os.path.dirname(config_path),
                                            "testcode.c")

        self.result_queue = queue.Queue()
        self.log_queue = queue.Queue()
        log_printer = ConsolePrinter()
        self.interactor = ProcessingTestInteractor(log_printer,
                                                   self.result_queue,
                                                   self.log_queue)

        (self.sections,
         self.local_bears,
         self.global_bears,
         targets) = gather_configuration(lambda *args: True,
                                         log_printer,
                                         ["--config", re.escape(config_path)])
        self.assertEqual(len(self.local_bears["default"]), 1)
        self.assertEqual(len(self.global_bears["default"]), 1)
        self.assertEqual(targets, [])

    def test_run(self):
        results = execute_section(self.sections["default"],
                                  self.global_bears["default"],
                                  self.local_bears["default"],
                                  self.interactor.print_results,
                                  self.interactor)
        self.assertTrue(results[0])

        local_results = self.result_queue.get(timeout=0)
        global_results = self.result_queue.get(timeout=0)
        self.assertTrue(self.result_queue.empty())

        self.assertEqual(len(local_results), 1)
        self.assertEqual(len(global_results), 1)
        # Result dict also returned
        # One file
        self.assertEqual(len(results[1]), 1)
        # One global bear
        self.assertEqual(len(results[2]), 1)

        local_result = local_results[0]
        global_result = global_results[0]

        self.assertEqual(str(local_result),
                         "Result:\n origin: 'LocalTestBear'\n file: None\n "
                         "line nr: None\n severity: 1\n'test msg'")
        file = (platform.system() == 'Windows' and
                self.testcode_c_path.lower() or self.testcode_c_path)
        self.assertEqual(str(global_result),
                         "Result:\n origin: 'GlobalTestBear'\n file: {}"
                         "\n line nr: None\n severity: 1\n'test "
                         "message'".format(repr(file)))

    def test_empty_run(self):
        results = execute_section(self.sections["default"],
                                  [],
                                  [],
                                  self.interactor.print_results,
                                  self.interactor)
        # No results
        self.assertFalse(results[0])
        # One file
        self.assertEqual(len(results[1]), 1)
        # No global bear
        self.assertEqual(len(results[2]), 0)

    def test_process_queues(self):
        mock_interactor = MessageQueueingInteractor()
        ctrlq = queue.Queue()

        # Append custom controlling sequences.

        # Simulated process 1
        ctrlq.put((CONTROL_ELEMENT.LOCAL, 1))
        ctrlq.put((CONTROL_ELEMENT.LOCAL_FINISHED, None))
        ctrlq.put((CONTROL_ELEMENT.GLOBAL, 1))

        # Simulated process 2
        ctrlq.put((CONTROL_ELEMENT.LOCAL, 2))

        # Simulated process 1
        ctrlq.put((CONTROL_ELEMENT.GLOBAL_FINISHED, None))

        # Simulated process 2
        ctrlq.put((CONTROL_ELEMENT.LOCAL_FINISHED, None))
        ctrlq.put((CONTROL_ELEMENT.GLOBAL, 1))
        ctrlq.put((CONTROL_ELEMENT.GLOBAL_FINISHED, None))

        process_queues(
            [DummyProcess(control_queue=ctrlq) for i in range(3)],
            ctrlq,
            {1: ["The first result."],
             2: ["The second result.", HiddenResult("t", "c")]},
            {1: ["The one and only global result."]},
            None,
            mock_interactor.print_results,
            Section(""))

        self.assertEqual(mock_interactor.get(), (["The first result."], None))
        self.assertEqual(mock_interactor.get(), (["The second result."], None))
        self.assertEqual(mock_interactor.get(),
                         (["The one and only global result."], None))
        self.assertEqual(mock_interactor.get(),
                         (["The one and only global result."], None))

        # No valid FINISH element in the queue
        ctrlq.put((CONTROL_ELEMENT.GLOBAL_FINISHED, None))

        process_queues(
            [DummyProcess(control_queue=ctrlq) for i in range(3)],
            ctrlq,
            {1: "The first result.", 2: "The second result."},
            {1: "The one and only global result."},
            None,
            mock_interactor.print_results,
            Section(""))
        with self.assertRaises(queue.Empty):
            mock_interactor.get()

    def test_create_process_group(self):
        p = create_process_group([StringConstants.python_executable,
                                  "-c",
                                  process_group_test_code],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        retval = p.wait()
        if retval != 0:
            for line in p.stderr:
                print(line, end='')
            raise Exception("Subprocess did not exit correctly")
        output = [i for i in p.stdout]
        p.stderr.close()
        p.stdout.close()
        pid, pgid = [int(i.strip()) for i_out in output for i in i_out.split()]
        if platform.system() != "Windows":
            # There is no way of testing this on windows with the current python
            # modules subprocess and os
            self.assertEqual(p.pid, pgid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
