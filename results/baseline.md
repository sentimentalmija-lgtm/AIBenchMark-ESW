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
- **Run (UTC)**: 2026-10-25T10:15:52.873222+00:00
- **Run status**: completed
- **Benchmark / Python**: 0.1.0 / 3.11.15
- **Compiler**: tcc / tcc version 0.9.27 (x86_64 Windows) (native)
- **Static analysis**: builtin / No cppcheck version recorded
- **Dataset SHA-256**: `96b16b5b2685cdb873c04e4f83b0123cf5af92226bd236aa44f20a1adcdede50`
- **Evaluator SHA-256**: `646f0c08078420291c26b5bb186f592e26a4afdb9af45bef9e8756d9870557a2`
- **Source revision**: `2ba0c258c4cccf00656833b0c4cfa96313d0450f`
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
| 1 | tier1_crc16 | c99 | 60%/20%/20% | PASS | 7/7 | 361 / 0 | Err:0, Warn:0 | **100.0** | 0.78s |
| 1 | tier1_q15_math | c99 | 60%/20%/20% | PASS | 16/16 | 270 / 0 | Err:0, Warn:0 | **100.0** | 0.76s |
| 1 | tier1_ring_buffer | c99 | 60%/20%/20% | PASS | 11/11 | 1474 / 0 | Err:0, Warn:0 | **100.0** | 0.78s |
| 2 | tier2_can_frame | c99 | 60%/20%/20% | PASS | 15/15 | 2600 / 0 | Err:0, Warn:0 | **100.0** | 0.86s |
| 2 | tier2_cobs_codec | c99 | 60%/20%/20% | PASS | 9/9 | 1186 / 0 | Err:0, Warn:0 | **100.0** | 0.79s |
| 2 | tier2_debounce_fsm | c99 | 60%/20%/20% | PASS | 7/7 | 1134 / 0 | Err:0, Warn:0 | **100.0** | 0.82s |
| 2 | tier2_fixed_control | c99 | 60%/20%/20% | PASS | 7/7 | 941 / 0 | Err:0, Warn:0 | **100.0** | 0.67s |
| 2 | tier2_tick_timer | c99 | 60%/20%/20% | PASS | 16/16 | 636 / 0 | Err:0, Warn:0 | **100.0** | 0.75s |
| 2 | tier2_windowed_watchdog | c99 | 60%/20%/20% | PASS | 15/15 | 1136 / 0 | Err:0, Warn:0 | **100.0** | 0.75s |
| 3 | tier3_flash_update | c99 | 60%/20%/20% | PASS | 7/7 | 1980 / 0 | Err:0, Warn:0 | **100.0** | 0.79s |
| 3 | tier3_i2c_sensor | c99 | 60%/20%/20% | PASS | 11/11 | 830 / 0 | Err:0, Warn:0 | **100.0** | 0.70s |
| 3 | tier3_spi_flash | c99 | 60%/20%/20% | PASS | 8/8 | 1913 / 0 | Err:0, Warn:0 | **100.0** | 0.71s |
| 4 | tier4_bitmask_fix | c99 | 60%/20%/20% | PASS | 5/5 | 462 / 0 | Err:0, Warn:0 | **100.0** | 0.73s |
| 4 | tier4_dma_buffer | c11 | 60%/20%/20% | PASS | 8/8 | 1590 / 0 | Err:0, Warn:0 | **100.0** | 0.73s |
| 4 | tier4_uart_frame_fix | c99 | 60%/20%/20% | PASS | 8/8 | 760 / 0 | Err:0, Warn:0 | **100.0** | 0.74s |