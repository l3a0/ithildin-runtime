"""The data module: daily bars from marketlake's sealed partitions, and quotes from unsealed ones.

#5 and #100 build it. docs/architecture/05-building-blocks.md places it, and its
reasoning lives in docs/design.md. It reads marketlake through `core.Bucket`,
which the data worker's entry point in `runtime` builds, so this package
imports no adapter and every mode runs the same code.

- `mirror` keeps the local copy of marketlake's bucket and checks every file
  against the manifest before a read.
- `settings` builds `DataSettings` from the `[data]` section of every
  strategy's tracked file.
- `source` is the point-in-time read, `LakeSource`, which implements
  `core.DataSource`.
- `segments` reads one decision's quotes from the capture segments of a day
  marketlake has not sealed.
- `reference` reads the security master and the two kind contracts, the three
  tracked files beside it.
- `spikes` flags a price judged wrong, beside the price.
- `stage` is data's stage, which stores each decision's snapshot.

It imports only the marketlake submodules the import rule names:
`lake.loader`, `lake.unsealed`, `lake.paths`, `lake.manifest`, `lake.actions`
and `lake.config`.
"""

from .mirror import Mirror
from .reference import SecurityMaster, load_master, load_price_kinds, load_quote_kinds
from .segments import QuoteLimits
from .settings import data_settings
from .source import DAILY, DataSettings, LakeSource, Reading
from .spikes import largest_multiple, spike_flags
from .stage import SNAPSHOT, STAGE, DataStage, differences

__all__ = [
    "DAILY",
    "SNAPSHOT",
    "STAGE",
    "DataSettings",
    "DataStage",
    "LakeSource",
    "Mirror",
    "QuoteLimits",
    "Reading",
    "SecurityMaster",
    "data_settings",
    "differences",
    "largest_multiple",
    "load_master",
    "load_price_kinds",
    "load_quote_kinds",
    "spike_flags",
]
