import os
from glob import glob

from setuptools import find_packages, setup

package_name = "scopio_ui"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages",
            ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
        # The gateway serves the rich SCOPIO frontend straight from
        # microscope/frontend/ (located via _repo.py) so there is one UI to
        # maintain -- no frontend files are shipped in this package.
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mark Konyk",
    maintainer_email="mark.konyk@gmail.com",
    description="Web UI gateway: a ROS 2 client that re-serves the graph to a browser.",
    license="MIT",
    entry_points={
        "console_scripts": [
            "ui_gateway = scopio_ui.gateway_node:main",
        ],
    },
)
