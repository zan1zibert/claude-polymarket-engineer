-- 0006 — drop forecast_scores (the scorer service has been removed).
--
-- Forward-only: 0003_forecast_scores.sql stays in place (never edit an
-- already-applied migration), this file just undoes it on any environment
-- that already ran it.

DROP TABLE IF EXISTS forecast_scores;
