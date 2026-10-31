import unittest

from calculator import add


class CalculatorTests(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-2, 3), 1)
        self.assertEqual(add(0, 0), 0)


if __name__ == "__main__":
    unittest.main()
