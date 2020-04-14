from setuptools import setup

setup(
    name="splitcopy",
    version='1.0.13',
    url="https://github.com/Juniper/splitcopy",
    author="Chris Jenn",
    author_email="jnpr-community-netdev@juniper.net",
    license="Apache 2.0",
    description="Improves file transfer rates when copying files to/from JUNOS/EVO/*nix hosts",
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    keywords=['ftp', 'ssh', 'scp', 'transfer'],
    py_modules=['splitcopy'],
    python_requires='>=3.4',
    install_requires=['junos-eznc>=2.3.0'],
    entry_points={
        'console_scripts': [
            'splitcopy=splitcopy:main',
        ],
    },
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: Apache Software License',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Intended Audience :: Information Technology',
        'Intended Audience :: System Administrators',
        'Intended Audience :: Telecommunications Industry',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Networking',
    ],
)
