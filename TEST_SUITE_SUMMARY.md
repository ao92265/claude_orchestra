# ProcessManager Test Suite Summary

## Overview
Comprehensive test suite for `process_manager.py` with 54 tests covering all critical functionality, edge cases, and concurrency scenarios.

## Test Results
- **Total Tests**: 54
- **Passed**: 52
- **Skipped**: 2 (due to implementation bugs)
- **Test Coverage**: All major code paths and edge cases

## Test Categories

### High Priority Tests (21 tests)

#### 1. Signal Handling (4 tests)
- ✅ SIGTERM initiates graceful shutdown
- ✅ SIGINT initiates graceful shutdown
- ✅ Double signal during cleanup is safely ignored
- ✅ Signal handling with no tracked processes

#### 2. Timeout with Graceful → Force Kill Escalation (3 tests)
- ✅ Graceful termination within timeout
- ✅ Force kill after timeout expires
- ✅ Timeout escalation with multiple processes

#### 3. Orphan Detection and Cleanup (6 tests)
- ✅ Detect and kill orphaned Claude CLI processes
- ✅ Detect and kill orphaned Orchestra processes
- ✅ Tracked processes are not killed as orphans
- ✅ Orphan detection filtered by project path
- ✅ Force kill orphans that don't terminate gracefully
- ✅ Handle NoSuchProcess during orphan detection

#### 4. Thread-Safe Process Tracking (4 tests)
- ✅ Concurrent process tracking (10 threads)
- ✅ Concurrent process untracking (10 threads)
- ✅ Concurrent track and untrack operations (20 threads)
- ✅ get_process() while another thread untracks

#### 5. Process Lifecycle: Track → Run → Untrack (4 tests)
- ✅ Basic lifecycle from track to untrack
- ✅ Lifecycle with explicit stop operation
- ✅ Multiple processes lifecycle
- ✅ is_running checks throughout lifecycle

### Medium Priority Tests (13 tests)

#### 6. Exception Handling (4 tests)
- ✅ Exception during process termination
- ⚠️ Exception during force kill (SKIPPED - reveals bug)
- ✅ Exception during orphan detection iteration
- ✅ Tracking process with None PID

#### 7. Process Already Exited (3 tests)
- ⚠️ Stop already-exited process (SKIPPED - deadlock bug)
- ✅ is_running check for exited process
- ✅ Race condition: process exits between poll() and terminate()

#### 8. Idempotent Operations (3 tests)
- ✅ Multiple untrack calls are safe
- ✅ Multiple stop calls are safe
- ✅ Track same process ID twice (replaces previous)

#### 9. Concurrent Operations (3 tests)
- ✅ Concurrent stop_process calls
- ✅ Concurrent is_running checks
- ✅ get_tracked_process_ids during modifications

### Edge Cases (13 tests)
- ✅ Process with no PID
- ✅ Process with zero PID
- ✅ Get nonexistent process
- ✅ Stop nonexistent process
- ✅ Untrack nonexistent process
- ✅ is_running for nonexistent process
- ✅ Empty string process ID
- ✅ Orphan detection with empty cmdline
- ✅ Orphan detection with None cmdline
- ✅ Very long timeout
- ✅ Zero timeout
- ✅ Negative timeout

### Integration Tests (3 tests)
- ✅ Full lifecycle with signal handling
- ✅ Mixed process states (running, timeout)
- ✅ Orphan detection while tracking processes

### Utility Tests (5 tests)
- ✅ get_tracked_count()
- ✅ get_tracked_process_ids()
- ✅ get_tracked_process_ids returns snapshot
- ✅ Singleton pattern returns same instance
- ✅ Singleton persists state across calls

## Bugs Discovered

### 🔴 Critical Bug #1: Deadlock in stop_process()
**Location**: `process_manager.py:104`

**Issue**: When stopping an already-exited process, `stop_process()` calls `untrack_process()` while holding `self._lock`. Since `untrack_process()` also tries to acquire `self._lock`, and the lock is not reentrant (RLock), this causes a deadlock.

**Code**:
```python
with self._lock:  # Lock acquired
    if process.poll() is not None:
        self.untrack_process(process_id)  # Tries to acquire same lock -> DEADLOCK!
```

**Fix Options**:
1. Use `threading.RLock()` instead of `Lock()`
2. Create internal untracking method that doesn't acquire lock
3. Release lock before calling `untrack_process()`

**Impact**: HIGH - Can cause application freeze when stopping exited processes

---

### 🔴 Critical Bug #2: Unprotected wait() after kill()
**Location**: `process_manager.py:119`

**Issue**: In the `TimeoutExpired` exception handler, `process.wait()` is called after `kill()` but is NOT protected by a try-except. If this wait() throws an exception, it propagates out of the function instead of being caught.

**Code**:
```python
try:
    process.terminate()
    process.wait(timeout=timeout)
    ...
except subprocess.TimeoutExpired:
    process.kill()
    process.wait(timeout=5)  # NOT protected! Exception will propagate
    ...
except Exception as e:  # Does NOT catch exceptions from other except blocks
    return False
```

**Fix**:
```python
except subprocess.TimeoutExpired:
    try:
        process.kill()
        process.wait(timeout=5)
        self.untrack_process(process_id)
        return True
    except Exception as e:
        logger.error(f"Error force killing process {process_id}: {e}")
        return False
```

**Impact**: HIGH - Can cause unexpected exceptions during process cleanup

---

### 🟡 Minor Bug #3: Stale PID accumulation
**Location**: `process_manager.py:track_process()`

**Issue**: When tracking a new process with the same process_id as an existing one, the old PID is not removed from `_tracked_pids`, leading to PID accumulation.

**Current Behavior**:
```python
def track_process(self, process_id, process):
    # Replaces process in dict but doesn't remove old PID
    self._tracked_processes[process_id] = process
    self._tracked_pids.add(process.pid)  # Adds new PID but keeps old one
```

**Fix**:
```python
def track_process(self, process_id, process):
    with self._lock:
        # Remove old PID if replacing existing process
        if process_id in self._tracked_processes:
            old_process = self._tracked_processes[process_id]
            if old_process.pid:
                self._tracked_pids.discard(old_process.pid)

        if process.pid:
            self._tracked_processes[process_id] = process
            self._tracked_pids.add(process.pid)
```

**Impact**: LOW - Functional impact is minimal, but could lead to memory leaks with many replacements

## Test Execution

Run all tests:
```bash
python3 -m pytest test_process_manager.py -v
```

Run specific test category:
```bash
python3 -m pytest test_process_manager.py::TestSignalHandling -v
python3 -m pytest test_process_manager.py::TestOrphanDetection -v
python3 -m pytest test_process_manager.py::TestThreadSafeProcessTracking -v
```

Run with coverage:
```bash
python3 -m pytest test_process_manager.py --cov=process_manager --cov-report=html
```

## Key Testing Techniques Used

1. **Mocking**: Extensive use of `unittest.mock` to simulate subprocess behavior without spawning real processes
2. **Thread Safety**: Concurrent testing with multiple threads to verify lock protection
3. **Edge Case Coverage**: Testing boundary conditions like zero/negative timeouts, None PIDs, empty strings
4. **Exception Simulation**: Using `side_effect` to simulate various error conditions
5. **Race Condition Testing**: Testing concurrent operations to find threading bugs
6. **Integration Testing**: Testing complete workflows with multiple components

## Recommendations

1. **Fix Critical Bugs**: Address the two deadlock/exception bugs before production use
2. **Add RLock**: Replace `Lock()` with `RLock()` for reentrant locking
3. **Add Nested Exception Handling**: Protect the kill+wait sequence
4. **Clean Up Stale PIDs**: Remove old PIDs when replacing process IDs
5. **Add Logging**: The tests verify logging calls are made appropriately
6. **Consider Timeout Defaults**: Very long/negative timeouts might need validation

## Test File Location
`/Users/aoreilly/Repos/claude_orchestra_dev/test_process_manager.py`

## Dependencies
- pytest
- unittest.mock (standard library)
- threading (standard library)
- subprocess (standard library)
