import os
from pathlib import Path
import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager
root = Path(__file__).resolve().parent
os.environ['MPLCONFIGDIR'] = str(root / '.mplconfig')
os.environ['IPYTHONDIR'] = str(root / '.ipython')
km=KernelManager(kernel_name='python3')
km.kernel_spec.argv=[str(root/'.venv/bin/python'),'-m','ipykernel_launcher','-f','{connection_file}']
nb=nbformat.read(root/'jfk_temperature_history.ipynb',as_version=4)
NotebookClient(nb, km=km, timeout=180, resources={'metadata':{'path':str(root)}}).execute()
nbformat.write(nb,root/'jfk_temperature_history.ipynb')
print('Notebook executed successfully')
