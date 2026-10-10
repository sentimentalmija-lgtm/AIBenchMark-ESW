# AIBenchMark-ESW Benchmark Report: `baseline`

### Summary Overview
- **Total Tasks**: 13
- **Compilation Rate**: 13/13 (100.0%)
- **Pass@1 (All Tests Passed)**: 13/13 (100.0%)
- **Overall AIBenchMark-ESW Score**: **100.00 / 100.0**

### Reproducibility
- **Run (UTC)**: 2026-10-08T00:42:30.749991+00:00
- **Run status**: completed
- **Benchmark / Python**: 0.1.0 / 3.14.3
- **Compiler**: clang / clang version 19.1.5 (-Os)
- **Static analysis**: builtin / No cppcheck version recorded
- **Dataset SHA-256**: `33924d0c6c9af95325974ac45aed9d4d07c6ed07541f2db7dc86699c1b9d67d3`
- **Evaluator SHA-256**: `82cd9fb5890b9c025dbe351eefef334c1177377566646562443eb894d1dc4e5b`
- **Source revision**: `082ec46bd40d2f4548945f7c28bba616a6d718ab`
- **Source had local changes**: True

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
| 1 | `tier1_crc16` | c99 | 60%/20%/20% | PASS | 7/7 | 130 / 0 | Err:0, Warn:0 | **100.0** | 0.87s |
| 1 | `tier1_q15_math` | c99 | 60%/20%/20% | PASS | 16/16 | 70 / 0 | Err:0, Warn:0 | **100.0** | 0.86s |
| 1 | `tier1_ring_buffer` | c99 | 60%/20%/20% | PASS | 11/11 | 384 / 0 | Err:0, Warn:0 | **100.0** | 0.91s |
| 2 | `tier2_cobs_codec` | c99 | 60%/20%/20% | PASS | 9/9 | 609 / 0 | Err:0, Warn:0 | **100.0** | 0.94s |
| 2 | `tier2_debounce_fsm` | c99 | 60%/20%/20% | PASS | 7/7 | 292 / 0 | Err:0, Warn:0 | **100.0** | 0.87s |
| 2 | `tier2_fixed_control` | c99 | 60%/20%/20% | PASS | 7/7 | 327 / 0 | Err:0, Warn:0 | **100.0** | 0.90s |
| 2 | `tier2_tick_timer` | c99 | 60%/20%/20% | PASS | 16/16 | 139 / 0 | Err:0, Warn:0 | **100.0** | 0.91s |
| 3 | `tier3_flash_update` | c99 | 60%/20%/20% | PASS | 7/7 | 735 / 0 | Err:0, Warn:0 | **100.0** | 0.99s |
| 3 | `tier3_i2c_sensor` | c99 | 60%/20%/20% | PASS | 11/11 | 368 / 0 | Err:0, Warn:0 | **100.0** | 0.96s |
| 3 | `tier3_spi_flash` | c99 | 60%/20%/20% | PASS | 6/6 | 861 / 0 | Err:0, Warn:0 | **100.0** | 0.97s |
| 4 | `tier4_bitmask_fix` | c99 | 60%/20%/20% | PASS | 5/5 | 141 / 0 | Err:0, Warn:0 | **100.0** | 0.98s |
| 4 | `tier4_dma_buffer` | c11 | 60%/20%/20% | PASS | 5/5 | 545 / 0 | Err:0, Warn:0 | **100.0** | 0.99s |
| 4 | `tier4_uart_frame_fix` | c99 | 60%/20%/20% | PASS | 7/7 | 361 / 0 | Err:0, Warn:0 | **100.0** | 0.80s |