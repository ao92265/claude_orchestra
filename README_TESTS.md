# ProcessManager Test Suite

## Quick Start

Run all tests:
```bash
python3 -m pytest test_process_manager.py -v
```

Expected output:
```
52 passed, 2 skipped in ~1 second
```

## Test Structure

The test suite is organized into logical test classes:

```
test_process_manager.py
├── High Priority Tests
│   ├── TestSignalHandling (4 tests)
│   ├── TestTimeoutEscalation (3 tests)
│   ├── TestOrphanDetection (6 tests)
│   ├── TestThreadSafeProcessTracking (4 tests)
│   └── TestProcessLifecycle (4 tests)
│
├── Medium Priority Tests
│   ├── TestExceptionHandling (4 tests)
│   ├── TestProcessAlreadyExited (3 tests)
│   ├── TestIdempotentOperations (3 tests)
│   └── TestConcurrentOperations (3 tests)
│
├── Edge Cases
│   └── TestEdgeCases (13 tests)
│
├── Integration Tests
│   └── TestIntegration (3 tests)
│
└── Utility Tests
    ├── TestSingleton (2 tests)
    └── TestUtilityMethods (3 tests)
```

## Running Specific Tests

### By Category
```bash
# Signal handling tests
python3 -m pytest test_process_manager.py::TestSignalHandling -v

# Thread safety tests
python3 -m pytest test_process_manager.py::TestThreadSafeProcessTracking -v

# Orphan detection tests
python3 -m pytest test_process_manager.py::TestOrphanDetection -v
```

### By Individual Test
```bash
python3 -m pytest test_process_manager.py::TestSignalHandling::test_sigterm_initiates_graceful_shutdown -v
```

### By Pattern
```bash
# All tests with "concurrent" in name
python3 -m pytest test_process_manager.py -k concurrent -v

# All tests with "timeout" in name
python3 -m pytest test_process_manager.py -k timeout -v
```

## Test Output Options

### Verbose mode
```bash
python3 -m pytest test_process_manager.py -v
```

### Show print statements
```bash
python3 -m pytest test_process_manager.py -v -s
```

### Show only failures
```bash
python3 -m pytest test_process_manager.py --tb=short
```

### Quiet mode (summary only)
```bash
python3 -m pytest test_process_manager.py -q
```

## Skipped Tests

Two tests are currently skipped due to implementation bugs:

1. **test_stop_already_exited_process**
   - Reveals deadlock bug when stopping already-exited processes
   - Issue: `stop_process()` calls `untrack_process()` while holding lock

2. **test_stop_process_with_exception_during_kill**
   - Reveals unprotected wait() call after kill()
   - Issue: Exception in TimeoutExpired handler is not caught

To run skipped tests anyway (they will fail):
```bash
python3 -m pytest test_process_manager.py --runxfail
```

## Understanding Test Results

### ✅ PASSED
Test executed successfully and all assertions passed.

### ⚠️ SKIPPED
Test was intentionally skipped (see reasons above).

### ❌ FAILED
Test failed - indicates either a bug in the implementation or test.

## Key Test Scenarios

### 1. Signal Handling
Tests verify graceful shutdown on SIGTERM/SIGINT:
- All tracked processes are terminated
- Duplicate signals are ignored
- Application exits cleanly

### 2. Timeout Escalation
Tests verify graceful → force kill escalation:
- Process terminates within timeout: only terminate() is called
- Process doesn't terminate: terminate() then kill() is called
- Multiple processes: each handled independently

### 3. Orphan Detection
Tests verify cleanup of abandoned processes:
- Claude CLI processes are detected
- Orchestra processes are detected
- Tracked processes are NOT killed
- Project path filtering works

### 4. Thread Safety
Tests verify concurrent operations don't corrupt state:
- 10-20 threads operating simultaneously
- Lock protection prevents race conditions
- Operations are atomic

### 5. Process Lifecycle
Tests verify complete workflow:
1. Track process
2. Verify it's running
3. Stop process
4. Verify it's untracked

## Mock Objects

Tests use `unittest.mock` to avoid spawning real processes:

```python
# Mock process that's running
mock_process.pid = 12345
mock_process.poll.return_value = None  # Running

# Mock process that's exited
mock_process.poll.return_value = 0  # Exited

# Mock process that times out
mock_process.wait.side_effect = subprocess.TimeoutExpired(...)
```

## Debugging Failed Tests

If a test fails:

1. Run with verbose output:
   ```bash
   python3 -m pytest test_process_manager.py::FailingTest -v
   ```

2. Add print debugging:
   ```bash
   python3 -m pytest test_process_manager.py::FailingTest -v -s
   ```

3. Check the test docstring for context:
   ```python
   def test_something(self):
       """This test verifies XYZ behavior..."""
   ```

4. Review TEST_SUITE_SUMMARY.md for known bugs

## Contributing New Tests

When adding tests:

1. Choose appropriate test class based on category
2. Use descriptive test names: `test_<what>_<when>_<expected>`
3. Add docstring explaining purpose
4. Use fixtures for common setup
5. Follow existing mock patterns
6. Test one thing per test
7. Include edge cases

Example:
```python
def test_stop_process_with_timeout_succeeds(self, process_manager, mock_process):
    """Test that process stops successfully within timeout period."""
    process_manager.track_process("test", mock_process)
    mock_process.wait.return_value = 0

    result = process_manager.stop_process("test", timeout=10)

    assert result is True
    mock_process.terminate.assert_called_once()
    assert process_manager.get_tracked_count() == 0
```

## Test Coverage

To generate coverage report:
```bash
pip3 install pytest-cov
python3 -m pytest test_process_manager.py --cov=process_manager --cov-report=html
open htmlcov/index.html  # View coverage report
```

## Files

- `test_process_manager.py` - Main test suite
- `TEST_SUITE_SUMMARY.md` - Detailed summary and bug reports
- `process_manager.py` - Module under test

## Dependencies

- Python 3.7+
- pytest (`pip3 install pytest`)
- No other external dependencies (uses standard library mocks)
