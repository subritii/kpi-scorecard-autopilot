-- The packet as HTML, built with packet_md from the same data (app/packet.py,
-- every value escaped). Stored per packet version so the approval page and the
-- leadership email show exactly the version that was checked and approved.
ALTER TABLE weekly_runs ADD COLUMN packet_html text;
