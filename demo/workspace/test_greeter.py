"""Tests for greeter.greet."""

import unittest

from greeter import greet


class GreetTests(unittest.TestCase):
    def test_comma_after_hello(self) -> None:
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_name_is_kept(self) -> None:
        self.assertEqual(greet("world"), "Hello, world!")


if __name__ == "__main__":
    unittest.main()
