# Models

All typed methods return Pydantic v2 models that inherit from `SiteWatchModel`.

Conventions:

- money is `float`, rounded to 2 decimal places;
- dates are `datetime.date`; timestamps are naive `datetime.datetime` in site-local wall-clock
  time (SiteWatch stores no UTC offset);
- IDs are `int`; text is trimmed and empty strings become `None`;
- every row-level model with `site_id` also carries `site_code` (for example `ICONTN-030`).

## SiteWatchModel

::: sitewatch_data_client.SiteWatchModel

## Shared

### SiteMetric

::: sitewatch_data_client.SiteMetric
    options:
      heading_level: 4

## Sites

### Site

::: sitewatch_data_client.Site
    options:
      heading_level: 4

### SiteFreshness

::: sitewatch_data_client.SiteFreshness
    options:
      heading_level: 4

## Sales

### DailySales

::: sitewatch_data_client.DailySales
    options:
      heading_level: 4

### SalesTotals

::: sitewatch_data_client.SalesTotals
    options:
      heading_level: 4

### HourlySales

::: sitewatch_data_client.HourlySales
    options:
      heading_level: 4

### ItemSales

::: sitewatch_data_client.ItemSales
    options:
      heading_level: 4

### TenderTotal

::: sitewatch_data_client.TenderTotal
    options:
      heading_level: 4

### Sale

::: sitewatch_data_client.Sale
    options:
      heading_level: 4

### SaleLine

::: sitewatch_data_client.SaleLine
    options:
      heading_level: 4

### StatusCount

::: sitewatch_data_client.StatusCount
    options:
      heading_level: 4

## Catalog

### Item

::: sitewatch_data_client.Item
    options:
      heading_level: 4

### ReportCategory

::: sitewatch_data_client.ReportCategory
    options:
      heading_level: 4

### Terminal

::: sitewatch_data_client.Terminal
    options:
      heading_level: 4

## Memberships

### MembershipPlan

::: sitewatch_data_client.MembershipPlan
    options:
      heading_level: 4

### MembershipActivity

::: sitewatch_data_client.MembershipActivity
    options:
      heading_level: 4

### MembershipActivityTotals

::: sitewatch_data_client.MembershipActivityTotals
    options:
      heading_level: 4

### NewMember

::: sitewatch_data_client.NewMember
    options:
      heading_level: 4

### MemberContact

::: sitewatch_data_client.MemberContact
    options:
      heading_level: 4

### MemberStatus

::: sitewatch_data_client.MemberStatus
    options:
      heading_level: 4

### MembershipEvent

::: sitewatch_data_client.MembershipEvent
    options:
      heading_level: 4

## Labor

### Employee

::: sitewatch_data_client.Employee
    options:
      heading_level: 4

### TimecardInterval

::: sitewatch_data_client.TimecardInterval
    options:
      heading_level: 4

### TimecardTotal

::: sitewatch_data_client.TimecardTotal
    options:
      heading_level: 4

### WeeklyHours

::: sitewatch_data_client.WeeklyHours
    options:
      heading_level: 4

### TimecardReport

::: sitewatch_data_client.TimecardReport
    options:
      heading_level: 4

### LaborStat

::: sitewatch_data_client.LaborStat
    options:
      heading_level: 4

### DailyLaborHours

::: sitewatch_data_client.DailyLaborHours
    options:
      heading_level: 4

### DailyProductivity

::: sitewatch_data_client.DailyProductivity
    options:
      heading_level: 4

### PunchSource

::: sitewatch_data_client.PunchSource
    options:
      heading_level: 4

### TimecardFlag

::: sitewatch_data_client.TimecardFlag
    options:
      heading_level: 4
