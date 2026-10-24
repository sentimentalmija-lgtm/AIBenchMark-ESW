# AIBenchMark-ESW Benchmark Report: baseline

### Summary Overview
- **Total Tasks**: 15
- **Compilation Rate**: 15/15 (100.0%)
- **Pass@1 (All Tests Passed)**: 15/15 (100.0%)
- **Overall AIBenchMark-ESW Score**: **100.00 / 100.0**

- **Tier-weighted score**: **100.00 / 100.0** (weights proportional to tier)

### Tier and category breakdown

| Model | Group type | Group | Tasks | Attempts | Mean | Pass@1 (%) | Delta vs baseline |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| baseline | by_tier | 1 | 3 | 3 | 100.0 | 100.0 | Unavailable |
| baseline | by_tier | 2 | 6 | 6 | 100.0 | 100.0 | Unavailable |
| baseline | by_tier | 3 | 3 | 3 | 100.0 | 100.0 | Unavailable |
| baseline | by_tier | 4 | 3 | 3 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | bug_fix_and_safety | 2 | 2 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | concurrency | 1 | 1 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | core_fundamentals | 3 | 3 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | device_drivers | 2 | 2 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | fixed_point_control | 1 | 1 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | flash_management | 1 | 1 | 100.0 | 100.0 | Unavailable |
| baseline | by_category | fsm_and_protocols | 5 | 5 | 100.0 | 100.0 | Unavailable |
### Reproducibility
- **Run (UTC)**: 2026-10-24T09:01:55.141018+00:00
- **Run status**: completed
- **Benchmark / Python**: 0.1.0 / 3.11.15
- **Compiler**: x86_64-linux-gnu-gcc-9 / x86_64-linux-gnu-gcc-9 (Ubuntu 9.4.0-1ubuntu1~20.04.2) 9.4.0 (-Os)
- **Static analysis**: builtin / No cppcheck version recorded
- **Dataset SHA-256**: `96b16b5b2685cdb873c04e4f83b0123cf5af92226bd236aa44f20a1adcdede50`
- **Evaluator SHA-256**: `49f87ab6e4356676bac578ae216d6ce892158ee269f52f0cfcd855cd1061e16e`
- **Source revision**: `df1afb55c430142285a7021d6dc4d8dd07a534a9`
- **Source had local changes**: False

### Dimensional Scores
| Dimension | Average Score | Weight |
| :--- | :--- | :--- |
| **Functional Correctness** | 100.00 / 100 | 60% |
| **Memory Efficiency** | 100.00 / 100 | 20% |
| **Safety & Code Rules** | 100.00 / 100 | 20% |
Measured dimensions: memory 15/15, safety 15/15 task attempts.

| **Composite Score** | **100.00 / 100** | 100% |

### Detailed Task Breakdown
Weights below are functional/memory/safety. Memory and safety contributions are scaled by the functional pass fraction.

| Tier | Task ID | Standard | Weights | Compile | Tests Passed | Flash/RAM (B) | Safety | Score | Time |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | tier1_crc16 | c99 | 60%/20%/20% | PASS | 7/7 | 227 / 0 | Err:0, Warn:0 | **100.0** | 0.43s |
| 1 | tier1_q15_math | c99 | 60%/20%/20% | PASS | 16/16 | 291 / 0 | Err:0, Warn:0 | **100.0** | 0.42s |
| 1 | tier1_ring_buffer | c99 | 60%/20%/20% | PASS | 11/11 | 598 / 0 | Err:0, Warn:0 | **100.0** | 0.52s |
| 2 | tier2_can_frame | c99 | 60%/20%/20% | PASS | 15/15 | 1242 / 0 | Err:0, Warn:0 | **100.0** | 0.57s |
| 2 | tier2_cobs_codec | c99 | 60%/20%/20% | PASS | 9/9 | 604 / 0 | Err:0, Warn:0 | **100.0** | 0.49s |
| 2 | tier2_debounce_fsm | c99 | 60%/20%/20% | PASS | 7/7 | 476 / 0 | Err:0, Warn:0 | **100.0** | 0.40s |
| 2 | tier2_fixed_control | c99 | 60%/20%/20% | PASS | 7/7 | 473 / 0 | Err:0, Warn:0 | **100.0** | 0.46s |
| 2 | tier2_tick_timer | c99 | 60%/20%/20% | PASS | 16/16 | 355 / 0 | Err:0, Warn:0 | **100.0** | 0.45s |
| 2 | tier2_windowed_watchdog | c99 | 60%/20%/20% | PASS | 15/15 | 521 / 0 | Err:0, Warn:0 | **100.0** | 0.49s |
| 3 | tier3_flash_update | c99 | 60%/20%/20% | PASS | 7/7 | 850 / 0 | Err:0, Warn:0 | **100.0** | 0.52s |
| 3 | tier3_i2c_sensor | c99 | 60%/20%/20% | PASS | 11/11 | 559 / 0 | Err:0, Warn:0 | **100.0** | 0.46s |
| 3 | tier3_spi_flash | c99 | 60%/20%/20% | PASS | 8/8 | 1262 / 0 | Err:0, Warn:0 | **100.0** | 0.53s |
| 4 | tier4_bitmask_fix | c99 | 60%/20%/20% | PASS | 5/5 | 350 / 0 | Err:0, Warn:0 | **100.0** | 0.40s |
| 4 | tier4_dma_buffer | c11 | 60%/20%/20% | PASS | 8/8 | 808 / 0 | Err:0, Warn:0 | **100.0** | 0.52s |
| 4 | tier4_uart_frame_fix | c99 | 60%/20%/20% | PASS | 8/8 | 603 / 0 | Err:0, Warn:0 | **100.0** | 0.41s |
