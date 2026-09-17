# Golden benchmark: golden-v1

- Status: **completed**
- Suite digest: `b1f735f5a8cb1184dad462cfbd0897dc937d7a97b52d5d3f89b5efce80d613c3`
- Metadata fingerprint: `166dd8378213af5e7c3ea5fdfac2b13aaff5f5c3a118c980d7f850b9640319a8`
- Commit: `2bb103693e789c5a48a175f2135a000976747acd`
- Working tree: `dirty` (status digest `a148834ef92871e9fc2c53564e257f586c9a9865ed22f825a51efcf087b8c2bd`)
- Backends: `stub` / `inline`
- Seed / repeats: `20260709` / `1`
- Pass rate: **0/1 (0.0%)**
- Wilson 95% interval: `0.0%` to `79.3%`

## Per stack

| Stack | Passed | Attempts | Pass rate | Wilson 95% | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| static | 0 | 1 | 0.0% | 0.0-79.3% | 0 |

## Per case

| Case | Passed | Attempts | Pass rate | Wilson 95% | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| restaurant-static | 0 | 1 | 0.0% | 0.0-79.3% | 0 |

## Failed expectations

| Case | Repeat | Status | Check | Expected | Actual / error |
| --- | ---: | --- | --- | --- | --- |
| restaurant-static | 1 | failed | build_status | completed | completed_no_go |
| restaurant-static | 1 | failed | verdict | go | no_go |
| restaurant-static | 1 | failed | score | &gt;= 60 | 34 |
