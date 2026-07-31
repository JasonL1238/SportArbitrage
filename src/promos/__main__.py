"""``python -m src.promos`` — collect sportsbook promotions."""
from __future__ import annotations

import sys

from src.promos.collector import main

sys.exit(main())
