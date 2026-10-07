# AIBenchMark-ESW Benchmark Report: `baseline`

### Summary Overview
- **Total Tasks**: 8
- **Compilation Rate**: 8/8 (100.0%)
- **Pass@1 (All Tests Passed)**: 8/8 (100.0%)
- **Overall AIBenchMark-ESW Score**: **100.00 / 100.0**

### Reproducibility
- **Run (UTC)**: 2026-10-07T11:54:26.867299+00:00
- **Run status**: completed
- **Benchmark / Python**: 0.1.0 / 3.14.4
- **Compiler**: gcc / gcc (Ubuntu 15.2.0-16ubuntu1) 15.2.0 (-Os)
- **Static analysis**: builtin / No cppcheck version recorded
- **Dataset SHA-256**: `7560070a1f197ce704b41ad2114ce060af0bf89a932ea7349606bb56b8c8f765`
- **Evaluator SHA-256**: `65ad65f38947faec8df281a163e82c44f7cd2b9b0149a16be3a834e5a937b9cb`
- **Source revision**: `Unavailable in installed distribution`
- **Source had local changes**: None

### Dimensional Scores
| Dimension | Average Score | Weight |
| :--- | :--- | :--- |
| **Functional Correctness** | 100.00 / 100 | 60% |
| **Memory Efficiency** | 100.00 / 100 | 20% |
| **Safety & Code Rules** | 100.00 / 100 | 20% |
| **Composite Score** | **100.00 / 100** | 100% |

### Detailed Task Breakdown
Weights below are functional/memory/safety. Memory and safety contributions are scaled by the functional pass fraction.

| Tier | Task ID | Standard | Weights | Compile | Tests Passed | Flash/RAM (B) | Safety | Score | Time |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | `tier1_crc16` | c99 | 60%/20%/20% | PASS | 6/6 | 696 / 0 | Err:0, Warn:0 | **100.0** | 1.48s |
| 1 | `tier1_q15_math` | c99 | 60%/20%/20% | PASS | 16/16 | 237 / 0 | Err:0, Warn:0 | **100.0** | 0.38s |
| 1 | `tier1_ring_buffer` | c99 | 60%/20%/20% | PASS | 9/9 | 567 / 0 | Err:0, Warn:0 | **100.0** | 0.40s |
| 2 | `tier2_cobs_codec` | c99 | 60%/20%/20% | PASS | 9/9 | 549 / 0 | Err:0, Warn:0 | **100.0** | 0.45s |
| 2 | `tier2_debounce_fsm` | c99 | 60%/20%/20% | PASS | 5/5 | 401 / 0 | Err:0, Warn:0 | **100.0** | 0.65s |
| 2 | `tier2_tick_timer` | c99 | 60%/20%/20% | PASS | 16/16 | 302 / 0 | Err:0, Warn:0 | **100.0** | 0.44s |
| 3 | `tier3_i2c_sensor` | c99 | 60%/20%/20% | PASS | 11/11 | 505 / 0 | Err:0, Warn:0 | **100.0** | 0.55s |
| 4 | `tier4_bitmask_fix` | c99 | 60%/20%/20% | PASS | 5/5 | 291 / 0 | Err:0, Warn:0 | **100.0** | 0.50s |
