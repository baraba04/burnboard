# How ccusage 20.0.48 prices usage (11 Nov 2026)

Result: the rules below reproduce ccusage's cost to the cent per local day and model on a real set of Claude Code transcripts (see the note on `-fast` model names under "Not verified"), and in every case of the made-up transcripts described under "Evidence". The one deliberate difference is in the section "Where the app differs from ccusage".

## Rules

1. Deduplicate by (message.id, requestId); a line that lacks either id is a call of its own. Of the lines of one message the one with the **most tokens** counts (input + output + both cache writes + cache read); on a tie the one with the higher cost (its costUSD, else the computed cost); the day is that line's. Streaming lines grow in output only, so on real data this is the last line (case 6).
2. Cache-write tokens are `cache_creation.ephemeral_5m_input_tokens + ephemeral_1h_input_tokens` when that object exists, else the top-level `cache_creation_input_tokens`.
3. Cost of a message = input x p_in + output x p_out + write5m x p_cw + write1h x p_cw1h + cache_read x p_cr, each model with its own prices from the LiteLLM list (`model_prices_and_context_window.json`).
4. Tiers (cases 3 and 4). A tier at exactly 200k (`*_above_200k_tokens`) splits **each token type at 200,000 tokens of its own**: the tokens up to 200,000 use the base price, only the tokens above use the tier price. Any other tier (`*_above_Nk_tokens`, in the price list haiku-5-5 with N = 100) switches **the whole message** when input + cache write + cache read is above N thousand; output does not count. Both tests are `>`: a message exactly on the threshold has the base price.
5. Fast mode (case 1): `usage.speed == "fast"` multiplies the whole cost of the call (all five token types) by the model's `provider_specific_entry.fast` in the list; when the entry has none, by ccusage's own table (ids starting `claude-opus-4-6` and `claude-opus-4-7`: 6, `claude-opus-4-8`: 2); else by 1. ccusage lists such a call as its own model `<model>-fast`.
6. `inference_geo` (case 2): ccusage ignores it. The app does not: it follows Anthropic's 1.1x for `"us"` (section "Where the app differs from ccusage").
7. `costUSD` (case 5): a call whose entry has a numeric top-level `costUSD` costs exactly that (zero, negative, tiny, and for a model without a price too). A `message.costUSD` is ignored. Default mode is `auto`; `--mode calculate` ignores costUSD (fast still multiplies), `--mode display` uses only costUSD.

## Why the earlier flat formula failed

It assumed cache read costs 0.1 x input for every model. That holds for older models only: fable-5-1 and fable-5-2 are at 0.025, opus-5-5 and sonnet-5-5 at 0.05. In Claude Code transcripts cache reads are usually most of the tokens, so the effective rate of a day follows its model mix. The 5-minute and 1-hour cache writes must also be priced apart.

## Evidence (black box, 11 Nov 2026)

`probe.py` writes made-up transcripts (one case per local day, own ids, because ccusage dedupes across all files) and runs `CLAUDE_CONFIG_DIR=<tmp> npx ccusage@20.0.48 daily --breakdown --json -z UTC` (the whole output: `probe-output-2026-11-11.txt`, 234 rows). `--offline` gives the same numbers except that its built-in list has no price for fable-5-2 and mythos-5-2 (cost 0). `ccusage claude daily --mode auto` equals plain `daily`. Two things the probe taught about ccusage itself: it reads only lines written without spaces (`separators=(",", ":")`, as Claude Code writes them), and it drops a whole line whose `speed` is not `standard` / `fast` or whose `costUSD` is not a number (null, string).

Prices used below: sonnet-4-5-20250929-v1:0 (the only direct Claude id with `_above_200k_tokens` keys): in 3e-6, out 1.5e-5, write5m 3.75e-6, write1h 6e-6, read 3e-7; above 200k: 6e-6, 2.25e-5, 7.5e-6, 1.2e-5, 6e-7. haiku-5-5: in 1e-7, out 5e-7, write5m 1.25e-7, write1h 2e-7, read 1e-8; above 100k: 5e-7, 2.5e-6, 6.25e-7, 1e-6, 5e-8.

### Cases 3 and 4: tiers above 200k, other tiers, and the threshold

| probe case | tokens | ccusage cost | reading |
|---|---|---|---|
| tier_s45_in_-1 / +0 / +1 | input 199,999 / 200,000 / 200,001 | 0.599997 / 0.600000 / 0.600006 | base up to and on 200,000; +1 token = 6e-6 extra, not 200,001 x 6e-6 (1.2): a split |
| tier_s45_cr_+1, c5_+1, c1_+1 | cache read / 5m write / 1h write 200,001 | 0.0600006 / 0.7500075 / 1.200012 | every type splits, the 1h write with its own above-200k price |
| tier_s45_out_+1 | input 10, output 200,001 | 3.0000525 | output splits too |
| tier_s45_each_below | 199,999 of input, output, 5m write, read | 4.40997795 | total 800k, no tier: each type counts alone |
| tier_s45_cw_split | 5m write 100,000 + 1h write 100,001 | 0.975006 | the two writes are not added up |
| tier_gem200_in_+1 (gemini-2.5-pro, 200k key) | input 200,001 | 0.2500025 | same split, so it is the threshold 200k, not the model |
| tier_h55_in_-1 / +0 / +1 | input 99,999 / 100,000 / 100,001 | 0.0099999 / 0.0100000 / 0.0500005 | the whole 100,001 at 5e-7; exactly 100,000 is base |
| tier_h55_split_+1 / split_0 | input, 5m, 1h writes 25,000 each, read 25,001 / 25,000 | 0.05437505 / 0.010875 | no field is large, the total (100,001) decides |
| tier_h55_out_+1 | input 10, output 100,001 | 0.0500015 | output not counted in the total |
| tier_h55_c1_+1 | 1h write 100,001 | 0.100001 | the 1h write has its own tier price |
| tier_gpt272_in_+0 / +1 / split_+1 (gpt-5.6, 272k key) | input 272,000 / 272,001 / 136,000 + read 136,001 | 1.088 / 2.176008 / 1.1968008 | whole-message and `>` for N = 272 as well |
| tier_o55_250k, tier_s55_1m, tier_s45_noprefix_250k | 250k, 900k, 250k with no tier key in the list | base price | no key, no tier |

Official source (platform.claude.com/docs/en/about-claude/pricing, section "Long context pricing", fetched 11 Nov 2026): Claude 4.6 and later models except Haiku 5.5 have no long-context surcharge; Haiku 5.5 is priced per request by prompt length (above 100,000 tokens, counting input, cache reads and cache writes, the whole request pays the higher price). That is the whole-message rule above, ccusage agrees. The old Sonnet 4 / 4.5 premium above 200k input tokens is no longer on the page; a search result for the earlier docs says the whole request was repriced there. ccusage instead splits per type, so for sonnet-4-5-20250929-v1:0 (which Claude Code does not write; Bedrock ids do) ccusage and Anthropic differ. The app follows ccusage.

### Case 1: fast mode

Same call (input, output, 5m write, 1h write, read = 1000 each) at standard and at `speed: "fast"`:

| model | standard | fast | factor | where the factor comes from |
|---|---|---|---|---|
| opus-5-5 | 0.0372 | 0.0744 | 2 | list `provider_specific_entry.fast` = 2 |
| fable-5-2 | 0.0742 | 0.1484 | 2 | list, fast = 2 |
| mythos-5-2, opus-5, opus-4-8 | 0.0742 / 0.04675 / 0.04675 | 0.1484 / 0.0935 / 0.0935 | 2 | list, fast = 2 |
| opus-4-6, opus-4-6-20260205 | 0.04675 | 0.2805 | 6 | not in the list; ccusage's own table (below) |
| opus-4-7, opus-4-7-20260416 | 0.04675 | 0.2805 | 6 | same |
| haiku-4-5, sonnet-4-5, sonnet-5, sonnet-5-5, sonnet-4-6, opus-4-5, fable-5, fable-5-1, mythos-5, mythos-5-1, mythos-preview, haiku-5-5 | | | 1 | no fast price |

All 25 direct Anthropic ids of the list were probed (`ratio_*` cases). The factor multiplies all five token types (opus-5-5 fast with input 250,000 + output 1,000: 2.04 = 2 x (1.0 + 0.02)). `speed` absent or standard: standard price; `speed: null` or an unknown string: the line is dropped. A top-level `speed` on the entry is ignored. A duplicate pair where the fast line and the standard line have the same tokens: the fast one wins in both orders (higher cost wins the tie).
The 6 for opus-4-6 and 4-7 exists only inside ccusage: `strings` on the binary shows an embedded table `normalized_prefix: {claude-opus-4-6: 6.0, claude-opus-4-7: 6.0, claude-opus-4-8: 2.0}`; the black-box numbers above prove it for the four ids, and the id `claude-opus-4-6-foo` also gets 6 (prefix). The app hard-codes the same table (`FAST_PREFIX`).
Official source (same pricing page, section "Fast mode pricing"): fast mode is listed for Fable 5.2, Mythos 5.2, Opus 5.5, Opus 5 and Opus 4.8 at twice the standard input and output price (opus-5-5 8 / 40 USD per million tokens against 4 / 20), applies across the full context window, and prompt caching and data residency multipliers stack on top. Opus 4.6 and 4.7 are not on the page any more; their 6x is ccusage's old table and is consistent with the earlier 6x fast price. Agrees with ccusage.

### Case 2: `inference_geo`

Same standard call, `usage.inference_geo` global vs "us": equal to the last digit for sonnet-5-5 (0.0186), opus-5-5 (0.0372), haiku-5-5 (0.000935), fable-5-2 (0.0742), opus-4-6, sonnet-4-5, haiku-4-5 and opus-4-5 (all 25 ids in `geo2_*`: the cost of a standard + a "us" call is exactly twice the standard one). "eu", "US", "not_available", "", null and an absent key also equal. "us" with fast (2x only, no 1.1), "us" with the haiku tier (0.0525005 both) the same. The list carries `provider_specific_entry.us = 1.1` for 4.6 and later models, and ccusage does not use it for Claude transcripts.
**ccusage and Anthropic disagree.** Official source (same page, section "Data residency pricing"): for Claude 4.6 and later models US-only inference (`inference_geo: "us"`) costs 1.1 times the standard price on every token category (and on the Haiku 5.5 tier prices). Decision (11 Nov 2026): where ccusage and Anthropic's official prices disagree, follow Anthropic; the app multiplies by 1.1 (see below). The real data had no such call when this was written.

### Case 5: `costUSD`

| probe case | entry | ccusage cost (default mode) |
|---|---|---|
| cost_none | no costUSD | 0.0186 (computed) |
| cost_odd / int / tiny / negative / zero | costUSD 123.456 / 5 / 1e-9 / -5.0 / 0 | the same number, 0 included |
| cost_unpriced_model | costUSD 5.0, model without a price | 5.0 |
| cost_two_msgs | costUSD 10.0 on one, none on the other | 10.0186 (mixed per call) |
| cost_in_message | `message.costUSD` 123.456 | 0.0186 (ignored) |
| cost_fast | costUSD 3.0 on a fast call | 3.0 |
| cost_null, cost_string ("7.5") | null, string | the line is dropped (no tokens either) |

`--mode calculate` gives 0.0186 for all of them (fast still doubles: 0.0744); `--mode display` counts only costUSD (computed costs are 0). The default of `ccusage daily` is `auto`.

### Case 6: which duplicate line counts

(sonnet-5-5: input 2e-6, output 1e-5.) Both orders of each pair give the same result, so the rule depends on values, not on position or timestamp:

| probe case | line A | line B | counted | reading |
|---|---|---|---|---|
| dup_first_is_highest | in 1000, out 100 | in 2000, out 50 | in 2000, out 50 | not the highest output |
| dq_in_vs_out | in 10000, out 1 | in 1000, out 5000 | in 10000, out 1 | total 10,001 against 6,000 |
| dq_read_vs_in | read 100000, in 1 | in 20000 | the read line | cache read counts |
| dq_write_vs_in | 5m write 50000, in 1 | in 30000 | the write line | cache write counts |
| dq_out_vs_in | out 3000 | in 1500 | the output line | |
| tie_sum | in 1000, out 100 | in 900, out 200 | in 900, out 200 | equal total 1100: higher cost |
| tie_costusd | costUSD 0.0001 | none, same tokens | cost 0.0186 | the computed cost is the higher one |
| tie_costusd_two | costUSD 1.0 | costUSD 2.0 | 2.0 | |
| tie_costusd_big_loses / wins | costUSD 5.0, in 1000 / in 2000 | in 2000 / in 1000, none | 0.004 / 5.0 | tokens first, then cost |
| dup_two_files, dup_file_order_vs_ts | | | same winner | across files and against timestamp order too |
| dayx_* | one line on day D (5000 in), one on D+1 (100 in) | | the cost lands on the day of the winning line | |
| dup_no_ids, dup_mid_only, dup_rid_only | | | both lines counted | both ids are needed |

Streaming lines of one message differ in output only, so the largest-total line is also the last and the highest-output one: the earlier rule "highest output" and the real-data match are consistent with this. Day difference: ccusage takes the day of the winning line; the app keeps the time (hour and day) of the first line. They differ only for a message that streams across local midnight.

## Consequences for the app

- `app/pricing.py` implements rules 3 to 7 from the price list (rule 6 differs from ccusage, see below) (tiers by threshold, fast factor from the list, then the hard-coded table); `app/build.py` keeps `speed` and `costUSD` per call and counts one line of a duplicated message by rule 1 (Cowork audit lines, which have no requestId, are matched by message.id alone; the time stays the first line's).
- Per model the price keys are `input_cost_per_token`, `output_cost_per_token`, `cache_creation_input_token_cost`, `cache_creation_input_token_cost_above_1hr`, `cache_read_input_token_cost`, plus the same with `_above_<N>k_tokens`, and `provider_specific_entry.fast`. Take them from the LiteLLM entry of the exact model id; never derive them from the input price.
- ccusage's built-in offline list has no price for fable-5-2 or mythos-5-2 (cost 0 offline). A price table shipped with the app must be kept current.
- `approx_pricing` now counts only the calls ccusage drops (a `speed` that is not standard / fast, a `costUSD` that is not a number): the app counts them at the computed price.

## Where the app differs from ccusage (Anthropic's prices win)

- `inference_geo` exactly `"us"`: 1.1x on every token type (input, output, 5-minute and 1-hour writes, cache read, and the Haiku 5.5 tier prices) for Claude Opus 4.6, Sonnet 4.6 and every later model (4.6, 4.7, 4.8, all 5.x, fable, mythos); no surcharge for anything earlier (3.x, opus-4, opus-4-1, opus-4-5, sonnet-4, sonnet-4-5, haiku-4-5). It multiplies with fast mode (opus-5-5 fast + us = 2 x 1.1). Other values ("global", "eu", "US", empty, null, absent) are priced as before; a call with its own `costUSD` costs exactly that. Source: the pricing page, "Data residency pricing" (case 2). `app/pricing.py` decides by model id (`_us_of`), because the shipped list keeps only the `fast` entry. `snapshot.meta[0].check.us_geo_calls` counts the calls with `"us"`, and `tests/compare_ccusage.py` prints it, so a gap to ccusage can be explained by it.

## Not verified

- A list entry that has tier keys at both 100k and 200k: no such model exists. The app uses the 200k split and ignores the others.
- Tiers at other N on a Claude model: none exist in the list. For non-Claude ids ccusage showed whole-message at N = 272 (tested), and N = 32, 128, 256, 512 gave no usable reading (those ids are priced by ccusage from another source, with no change at the threshold).
- Whether ccusage reads the haiku-5-5 100k tier from the LiteLLM keys or from the models.dev data it also downloads (`tiers: size 100000`); the numbers are the same in both. Which source ccusage would use for a future Claude model with a 200k key and a models.dev tier is not known.
- Which of the list's `fast` and ccusage's own table wins when both exist and differ (today they agree for opus-4-8).
- Model ids. ccusage matches fuzzily and by provider prefix: `claude-opus-4-6-foo` is priced like opus-4-6, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` at that Bedrock key's own price (1.1 x). The app prices only exact ids (after removing a provider prefix and a date or `-v1:0`) and leaves others unpriced or at the base id's price. Not one of the six cases; the real data has none.
- `-fast` model names: ccusage reports a fast call under `<model>-fast`; the app keeps one model name, so a day with a fast call differs per cell from ccusage although the day total is equal.
- ccusage is a Rust binary; the rules come from black-box runs and strings in the binary, not from reading source.

## Reproduce

Your own data: `python3 tests/compare_ccusage.py` prices your transcripts with the app's code and with ccusage (it downloads ccusage from npm) and prints how many (day, model) cells agree to the cent.
Cases of this file: `python3 probe.py <empty folder>` (add `--offline`, `--claude`, or `"--pass=--mode calculate"`); the script prints one row per case and model. The price list ccusage used is cached in `~/.cache/ccusage/http-cache/`.
