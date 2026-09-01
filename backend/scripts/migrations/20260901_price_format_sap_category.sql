-- Adds an optional explicit SAP export category for price formats.

ALTER TABLE price_formats
    ADD COLUMN IF NOT EXISTS sap_category VARCHAR(32);
