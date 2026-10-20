# Registry data attribution

The `greenhouse.yml`, `lever.yml`, and `ashby.yml` tenant registries in this
directory are derived from the freehire project
(https://github.com/strelov1/freehire, `sources/` directory), MIT License,
Copyright (c) 2026 freehire contributors. Snapshot: 2026-09-02.

They are plain lists of `{company, board}` public job-board identifiers, used
here for board DISCOVERY only: when a job at an unknown company surfaces from
an aggregator, jobkit looks up the company's direct board token so it can
switch to polling the employer's own public board API.
