import sys
from pathlib import Path

# Add the repo root to the Python path so that data_gen can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))
