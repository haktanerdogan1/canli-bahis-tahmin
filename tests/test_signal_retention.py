"""Isolated regression tests: never import the live app or production DB."""
import ast
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class SignalRetention(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE consensus_predictions (id INTEGER PRIMARY KEY, decision TEXT, outcome TEXT, weighted_probability REAL)')
        self.capacity = load_function('app/core/orchestrator.py', '_kapasite_kontrolu',
                                      {'AYNI_ANDA_ACIK_KAPASITE': 10})

    def tearDown(self):
        self.db.close()

    def test_stronger_candidate_never_evicts_published_signal(self):
        self.db.executemany('INSERT INTO consensus_predictions VALUES (?, ?, ?, ?)',
                            [(i, 'signal', None, .5 + i / 100) for i in range(10)])
        before = self.db.execute('SELECT * FROM consensus_predictions').fetchall()
        for probability in (.1, .99, None):
            self.assertFalse(self.capacity(self.db.cursor(), probability))
        self.assertEqual(before, self.db.execute('SELECT * FROM consensus_predictions').fetchall())

    def test_resolved_signals_free_capacity(self):
        self.db.executemany('INSERT INTO consensus_predictions VALUES (?, ?, ?, ?)',
                            [(i, 'signal', None if i < 9 else 'WON', .8) for i in range(10)])
        self.assertTrue(self.capacity(self.db.cursor(), .9))

    def test_automatic_cleanup_never_deletes_a_void(self):
        cleanup = load_function('settlement.py', 'delete_unresolvable_void', {})
        self.db.execute("INSERT INTO consensus_predictions VALUES (1, 'signal', 'VOID', .7)")
        self.assertEqual(cleanup(), 0)
        self.assertEqual(self.db.execute('SELECT outcome FROM consensus_predictions').fetchone()[0], 'VOID')


if __name__ == '__main__':
    unittest.main()
