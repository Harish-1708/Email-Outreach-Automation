# Optional per-campaign overrides

A campaign needs NO file in this folder to work — it exists purely by
having a `templates/<campaign_name>/` folder, and runs on the defaults in
`config/settings.yaml`.

Only add a file here (named exactly `<campaign_name>.yaml`, matching the
templates folder name) if that specific campaign needs to differ from the
defaults. You only need to specify what's different — everything else is
inherited automatically.

Example — a campaign that needs a lower daily limit and a shorter,
2-stage sequence instead of the default 5:

```yaml
# config/campaigns/DudeRobe_Creator_Outreach.yaml
sending:
  daily_limit: 50

stages:
  - name: intro
    template_prefix: intro
    wait_days_after_previous: 0
  - name: followup1
    template_prefix: followup1
    wait_days_after_previous: 5
```

Anything you don't mention here — `variants`, `reply_monitor`, the rest of
`sending`, etc. — still comes from `config/settings.yaml`'s
`default_campaign_settings`.
