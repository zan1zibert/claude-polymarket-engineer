-- 0007 — relevance_checks.probability (the relevance filter moves to Jev).
--
-- Jev answers each candidate with P(same event) instead of a boolean plus a
-- one-sentence explanation, and the worker thresholds that probability in
-- code. Storing the raw value lets the threshold be re-tuned from history
-- without re-running inference. NULL for rows written by the old Groq filter
-- and for failed requests.

ALTER TABLE relevance_checks ADD COLUMN IF NOT EXISTS probability DOUBLE PRECISION;
