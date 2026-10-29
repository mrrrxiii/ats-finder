# ATS Support and Evidence Report

Status verified on 2026-10-28. “Supported” means this repository contains a
provider-specific adapter that uses a documented public interface and refuses
to advance a complete-snapshot baseline when response shape or pagination
cannot be validated. It does not mean every imported customer board is live.

Runtime scan outcomes remain separate:

- `SUPPORTED`, `UNSUPPORTED`, and `UNKNOWN` describe adapter coverage.
- `SUCCESS`, `FAILED`, `INCOMPLETE`, and `NOT_SCANNED` describe a particular
  board's latest scan state.

## Implemented providers

| Provider | Public interface and completeness proof | Publication evidence | Live board tested |
|---|---|---|---|
| Greenhouse | [Job Board API](https://docs.greenhouse.io/job-board): one complete `/jobs?content=true` response; require `meta.total == len(jobs)`. Prospect posts with null `internal_job_id` are counted for completeness but excluded as non-position records. | Relevant candidates may use documented detail `first_published`, stored as `ORIGINAL_PUBLICATION`. `updated_at` is never used. A detail failure only removes optional timestamp evidence; it does not turn a complete list into an empty/partial snapshot. | `greenhouse` |
| Lever | [Postings API](https://github.com/lever/postings-api): global and EU `skip`/`limit` pagination; stop only on a short page, reject repeated IDs, and enforce the configured page ceiling. | The documented response contract has no publication timestamp. Undocumented `createdAt` is deliberately ignored. Snapshot comparison only. | `leverdemo` |
| Ashby | [Public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api): one response containing all currently published postings; require API version `1`, stable URL/response identity agreement, and unique IDs; exclude `isListed=false`. | `publishedAt` is documented as when the job was **last published**, so it is stored as `REPUBLICATION`, not original creation. | `Ashby` |
| Workable | [Public account jobs API](https://workable.readme.io/reference/jobs-1): one documented account response containing its public jobs with `details=true`; require valid account/jobs shapes and unique shortcodes. | `published_on` is a date without a documented time or timezone. It is validated but not converted to midnight or used for a rolling 24-hour claim. `created_at` and `updated_at` are not publication evidence. Snapshot comparison only. | `clickview` |

On 2026-10-28, the opt-in bounded integration suite completed successfully for
all four rows above: **4 passed**. The tests assert a certified complete result,
at least one request, and unique stable job identifiers; they do not assert a
board-specific minimum job count because a valid public board may be empty.

## Unsupported requested providers

The following requested platforms are imported and reported, but no adapter is
claimed. A provider is left unsupported when this implementation has not
verified a provider-wide public interface, stable identity extraction, and a
reliable way to certify a complete current snapshot without credentials or
access-control circumvention.

| Provider | Current status |
|---|---|
| Workday | Unsupported |
| BambooHR | Unsupported |
| Rippling | Unsupported |
| JazzHR | Unsupported |
| Breezy HR | Unsupported |
| Recruitee | Unsupported |
| Teamtailor | Unsupported |
| iCIMS | Unsupported |
| Oracle / Oracle Cloud HCM / Taleo | Unsupported |
| SAP SuccessFactors | Unsupported |
| Dayforce | Unsupported |
| ADP | Unsupported |
| Jobvite | Unsupported |

Other platforms in the dataset are handled the same way. They remain stored
with their canonical URLs and the reason `No verified adapter is implemented
for this ATS provider`, making future coverage work auditable.

## Dataset import verification

The full Latmay dataset import was exercised on 2026-10-28 using resolved commit
`ff47d017ce3e0018e164eed1c4f820d07ec9fb40` and CSV SHA-256
`3ced7fbb2c7aca03b95493fc937937faab80215ca76624cae8f3b23c793f9ff0`.

After URL normalization and provider/board identity deduplication:

| Import result | Count |
|---|---:|
| Parsed unique sources | 69,610 |
| Supported stored sources | 35,659 |
| Unsupported stored sources | 33,226 |
| Removed-provider rows skipped | 725 |
| Malformed rows | 0 |

Supported-source counts in that import were Greenhouse 14,335, Lever 8,540,
Ashby 5,709, and Workable 7,075. Dataset inclusion is not a live-scan success
claim; individual boards can subsequently be failed, incomplete, or never
scanned.

The dataset card currently shows an example filename different from the actual
repository file. The importer uses the verified repository filename
`ats_career_page_urls.csv` and captures Hugging Face's resolved commit header.

## Evidence boundaries

- A trustworthy old timestamp stays `VERIFIED_OLD`; it is not reclassified as
  newly detected merely because its ID first appeared locally.
- A future timestamp becomes `VERIFIED_TIME_INVALID`.
- Snapshot newness means “absent from the preceding successful complete board
  snapshot,” not “published by the employer at first-seen time.”
- The snapshot gap is frozen at detection for auditability. Eligibility also
  expires 24 hours after detection in the current result view.
- Reappearance under the same stable ID is `REAPPEARED`.
- A highly similar new ID is linked as `POTENTIAL_REPOST`; it does not enter the
  snapshot-new results unless independent verified publication evidence
  qualifies it.
- Failed and incomplete attempts store scan metadata but no presence
  observations and never become comparison baselines.
