"""Entry point for cPanel "Setup Python App" (Passenger)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from portal import create_app  # noqa: E402

application = create_app(instance_path=os.path.join(HERE, "instance"))
