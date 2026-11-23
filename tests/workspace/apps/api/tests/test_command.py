import unittest
from contextlib import redirect_stdout
from io import StringIO

from api import main


class TestCommand(unittest.TestCase):
    def test_output(self):
        with redirect_stdout(StringIO()) as output:
            main()
        self.assertEqual(output.getvalue(), "api drvlang\n")
