#!/usr/bin/env python3
""" Copyright 2018 Juniper Networks, Inc. All rights reserved.
    Licensed under the Juniper Networks Script Software License (the "License").
    You may not use this script file except in compliance with the License, 
    which is located at
    http://www.juniper.net/support/legal/scriptlicense/
    Unless required by applicable law or otherwise agreed to in writing by the
    parties, software distributed under the License is distributed on an "AS IS"
    BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or 
    implied.

    splits a given file into pieces in a tmp directory, copies these to a junos
    host then reassembles them. Tested to be 15x faster to transfer an 845MB
    file than regular ftp/scp.

    Requires 'system services ssh' configuration on remote host.
    If using ftp to copy files (default) then 'system services ftp' is also
    required.

    Requires python 3.4+ to run.
        3x faster in 3.6 than 3.4

    install required module via:
        pip3 install junos-eznc

    Script overhead is 5-10 seconds on 64bit RE's, longer on RE2000's
    and PPC based models like MX80.
    This includes authentication, sha1 generation/comparison,
    disk space check, file split and join.
    It will be slower than ftp/scp for small files as a result.

    Because it opens many simultaneous connections
    if the router has limits set like this:

    system {
        services {
            ssh { # or ftp
                connection-limit 10;
                rate-limit 10;
            }
        }
    }

    The script will deactivate these limits so it can proceed
"""

import sys
# prevent Exception due to python3 module 'asyncio'
if (sys.version_info[0] < 3 or
        (sys.version_info[0] == 3 and sys.version_info[1] < 4)):
    print('Python 3.4 or later required, faster with 3.6+')
    sys.exit(1)
import argparse
import asyncio
import os
import contextlib
import datetime
import fnmatch
import functools
import getpass
import re
import shutil
import tempfile
import subprocess
import paramiko
import scp
from jnpr.junos import Device
from jnpr.junos.utils.ftp import FTP
from jnpr.junos.utils.scp import SCP
from jnpr.junos.utils.start_shell import StartShell

def main():
    """
    Generic main() statement
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('filepath',
                        help='Path to filename to work on')
    parser.add_argument('host',
                        help='remote host to connect to')
    parser.add_argument('user',
                        help='user to authenticate on remote host')
    parser.add_argument('-p', '--password', nargs=1,
                        help='password to authenticate on remote host')
    parser.add_argument('-d', '--remotedir', nargs=1,
                        help='remote host directory to put file')
    parser.add_argument('-s', '--scp', action='store_const', const='scp',
                        help='use scp to copy files instead of ftp')
    args = parser.parse_args()

    if not args.user:
        parser.error('must specify a username')

    if not args.host:
        parser.error('must specify a remote host')

    host = args.host
    user = args.user

    if not args.password:
        password = getpass.getpass(prompt='Password: ', stream=None)
    else:
        password = args.password[0]

    if args.remotedir:
        remotedir = args.remotedir[0]
    else:
        remotedir = '/var/tmp'

    if not os.path.isfile(args.filepath):
        print('source file {} does not exist - cannot proceed'
              .format(args.filepath))
        sys.exit(1)

    if re.search('/', args.filepath):
         file_name = args.filepath.rsplit('/', 1)[1]
    else:
         file_name = args.filepath

    file_path = os.path.abspath(args.filepath)
    file_size = os.path.getsize(file_path)
    start_time = datetime.datetime.now()

    print('checking remote port(s) are open...')
    if not port_check(host, 'ssh', '22'):
        sys.exit(1)
    if args.scp:
        copy_func = scp_put
    else:
        if port_check(host, 'ftp', '21'):
            copy_func = ftp_put
        else:
            copy_func = scp_put

    with tempdir():
        # connect to host
        dev = Device(host=host, user=user, passwd=password)
        try:
            with StartShell(dev) as ss:
                if os.path.isfile(file_path + '.sha1'):
                    sha1file = open(file_path + '.sha1', 'r')
                    orig_sha1 = sha1file.read().rstrip()
                else:
                    print('sha1 not found, generating sha1...')
                    try:
                        sha1_str = subprocess.check_output(['shasum',
                                                            file_path]).decode()
                    except subprocess.SubprocessError as err:
                        print('an error occurred generating a local sha1, '
                              'the error was:\n{}'
                              .format(err))
                        sys.exit(1)
                    orig_sha1 = sha1_str.split()[0]

                if copy_func == ftp_put:
                    split_size = str(divmod(file_size, 40)[0])
                else:
                    # check if JUNOS running BSD10+
                    # scp to non-occam creates 3 pids per chunk
                    # scp to occcam creates 2 pids per chunk
                    # each uid can have max of 64 processes
                    # values here should leave ~24 pid headroom
                    ver = ss.run('uname -i')
                    if ss.last_ok:
                        verstring = (ver[1].split('\n')[1].rstrip())
                        if re.match(r'JNPR', verstring):
                            split_size = str(divmod(file_size, 20)[0])
                        else:
                            split_size = str(divmod(file_size, 13)[0])
                    else:
                        # fallback to lower values
                        split_size = str(divmod(file_size, 13)[0])

                print('splitting file...')
                try:
                    subprocess.call(['split', '-b', split_size, file_path,
                                     file_name],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=600)
                except subprocess.TimeoutExpired:
                    print('splitting the file timed out after 10 mins')
                    sys.exit(1)
                except subprocess.SubprocessError as err:
                    print('an error occurred while splitting the file, '
                          'the error was:\n{}'
                          .format(err))
                    sys.exit(1)

                sfiles = []
                for sfile in os.listdir('.'):
                    if fnmatch.fnmatch(sfile, '{}*'.format(file_name)):
                        sfiles.append(sfile)

                # begin pre transfer checks, check if remote directory exits
                ss.run('test -d {}'.format(remotedir))
                if not ss.last_ok:
                    print('remote directory specified does not exist')
                    sys.exit(1)

                # cleanup previous tmp directory if found
                if not remote_cleanup(ss, remotedir, file_name):
                    sys.exit(1)

                # remote file system storage check
                print('checking remote storage...')
                df_tuple = ss.run('df {}'.format(remotedir))
                if not ss.last_ok:
                    print('failed to determine remote disk space available')
                    sys.exit(1)
                avail_blocks = (df_tuple[1].split('\n')[2].split()[3].rstrip())
                avail_bytes = int(avail_blocks) * 512
                if file_size * 2 > avail_bytes:
                    print('not enough space on remote host. Available space '
                          'must be 2x the original file size because it has to '
                          'store the file chunks and the whole file at the '
                          'same time')
                    sys.exit(1)

                # end of pre transfer checks, create tmp directory
                ss.run('mkdir {}/splitcopy_{}' .format(remotedir, file_name))
                if not ss.last_ok:
                    print('unable to create the tmp directory on remote host')
                    sys.exit(1)

                # begin connection/rate limit check and transfer process
                if copy_func == ftp_put:
                    limit_check(ss, 'ftp')
                    kvargs = {'callback': UploadProgress(file_size).handle}
                else:
                    limit_check(ss, 'ssh')
                    kvargs = {'progress': True, 'socket_timeout': 30.0}

                print('starting transfer...')
                loop = asyncio.get_event_loop()
                tasks = []
                loop_start = datetime.datetime.now()
                for sfile in sfiles:
                    task = loop.run_in_executor(None,
                                                functools.partial(copy_func,
                                                                  dev,
                                                                  sfile,
                                                                  file_name,
                                                                  remotedir,
                                                                  **kvargs))
                    tasks.append(task)
                try:
                    loop.run_until_complete(asyncio.gather(*tasks))
                except scp.SCPException as err:
                    print('scp returned the following error:\n{}'
                          .format(err))
                    remote_cleanup(ss, remotedir, file_name)
                    sys.exit(1)
                except KeyboardInterrupt:
                    remote_cleanup(ss, remotedir, file_name)
                    sys.exit(1)
                loop.close()
                loop_end = datetime.datetime.now()

                # end transfer, combine chunks
                print('joining files...')
                ss.run('cat {}/splitcopy_{}/* > {}/{}'
                       .format(remotedir, file_name, remotedir, file_name),
                       timeout=600)
                if not ss.last_ok:
                    print('failed to combine chunks on remote host')
                    sys.exit(1)

                # remove remote tmp dir
                remote_cleanup(ss, remotedir, file_name)

                # generate a sha1 for the combined file, compare to sha1 of src
                print('generating remote sha1...')
                ss.run('ls {}/{}'.format(remotedir, file_name))
                if ss.last_ok:
                    sha1_tuple = ss.run('sha1 {}/{}'
                                        .format(remotedir, file_name),
                                        timeout=300)
                    if ss.last_ok:
                        new_sha1 = (sha1_tuple[1].split('\n')[1].split()[3]
                                    .rstrip())
                        if orig_sha1 == new_sha1:
                            print('local and remote sha1 match\nfile has been '
                                  'successfully copied to {}:{}/{}'
                                  .format(host, remotedir, file_name))
                        else:
                            print('file has been copied to {}:{}/{}, but the '
                                  'local and remote sha1 do not match - '
                                  'please retry'
                                  .format(host, remotedir, file_name))
                            remote_cleanup(ss, remotedir, file_name)
                            sys.exit(1)
                    else:
                        print('remote sha1 verification didnt complete, '
                              'manually check the output of "sha1 <file>" and '
                              'compare against {}'
                              .format(orig_sha1))
                else:
                    print('file {}:{}/{} not found! please retry'
                          .format(host, remotedir, file_name))
                    remote_cleanup(ss, remotedir, file_name)
                    sys.exit(1)

        except paramiko.ssh_exception.BadAuthenticationType:
            print('authentication type used isnt allowed by the host')
            sys.exit(1)

        except paramiko.ssh_exception.AuthenticationException:
            print('ssh authentication failed')
            sys.exit(1)

        except paramiko.ssh_exception.BadHostKeyException:
            print('host key verification failed. delete the host key in '
                  '~/.ssh/known_hosts and retry')
            sys.exit(1)

        except paramiko.ssh_exception.ChannelException as err:
            print('an attempt to open a new ssh channel failed. '
                  ' error code returned was:\n{}'
                  .format(err))
            sys.exit(1)

        except paramiko.ssh_exception.SSHException as err:
            print('an ssh error occurred')
            sys.exit(1)

        except KeyboardInterrupt:
            remote_cleanup(ss, remotedir, file_name)
            sys.exit(1)

        # and.... we are done
        dev.close()
        end_time = datetime.datetime.now()
        time_delta = end_time - start_time
        transfer_delta = loop_end - loop_start
        print('data transfer = {}\ntotal runtime = {}'
              .format(transfer_delta, time_delta))


def ftp_put(dev, sfile, file_name, remotedir, **ftpargs):
    """ copies file to remote host via ftp
    Args:
        dev - the ssh connection handle
        sfile(str) - name of the file to copy
        file_name(str) - part of directory name
    Returns:
        None
    Raises:
        None
    """
    with FTP(dev, **ftpargs) as ftp:
        ftp.put(sfile, '{}/splitcopy_{}/'.format(remotedir, file_name))


def scp_put(dev, sfile, file_name, remotedir, **scpargs):
    """ copies file to remote host via scp
    Args:
        dev - the ssh connection handle
        sfile(str) - name of the file to copy
        file_name(str) - part of directory name
    Returns:
        None
    Raises:
        None
    """
    with SCP(dev, **scpargs) as scp:
        scp.put(sfile, '{}/splitcopy_{}/'.format(remotedir, file_name))

class UploadProgress(object):
    """ class which ftp module calls back to after each block has been sent
    """

    def __init__(self, file_size):
        """ Initialise the class
        """
        self.block_size = 0
        self.file_size = file_size
        self.last_percent = 0

    def handle(self, arg=None):
        """ For every 10% of data transferred, notifies the user
        Args:
            self
        Returns:
            None, just prints progress
        Raises:
            None
        """
        self.block_size += 8192
        percent_done = round((self.block_size / self.file_size) * 100)
        if self.last_percent != percent_done:
            self.last_percent = percent_done
            if percent_done %10 == 0:
                print('{}% done'.format(str(percent_done)))


@contextlib.contextmanager
def change_dir(newdir, cleanup=lambda: True):
    """ cds into temp directory.
        Upon script exit, changes back to original directory
        and calls cleanup() to delete the temp directory
    Args:
        newdir(str) - path to temp directory
        cleanup(?) - pointer to cleanup function ?
    Returns:
        None
    Raises:
        None
    """
    prevdir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(prevdir)
        cleanup()


@contextlib.contextmanager
def tempdir():
    """
    creates a temp directory
    defines how to delete directory upon script exit
    Args:
        None
    Returns:
        dirpath(str): path to temp directory
    Raises:
        None
    """
    dirpath = tempfile.mkdtemp()

    def cleanup():
        """ deletes temp dir
        """
        shutil.rmtree(dirpath)
    with change_dir(dirpath, cleanup):
        yield dirpath


def port_check(host, proto, port, failure=False):
    """ checks if a port is open on remote host
    Args:
        host(str) - host to connect to
        proto(str) - protocol to connect with
        port(str) - port to connect to
    Returns:
        True if port is open
        False if port is closed
    Raises:
        subprocess.TimeoutExpired if timeout occurs
        subprocess.SubprocessError for generic subprocess errors
    """

    try:
        if subprocess.call(['nc', '-z', host, port],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           timeout=10):
            print('remote {} port {} isnt open'
                  .format(proto, port))
            failure = True
    except subprocess.TimeoutExpired:
        print('{} port check timed out after 10 seconds'
              ', is the host reacheable and {} enabled?'
              .format(proto, proto))
        failure = True
    except subprocess.SubprocessError as err:
        print('an error occurred during remote {} port check, '
              'the error was:\n{}'
              .format(proto, err))
        failure = True

    if failure:
        return False
    else:
        return True

def limit_check(ss, con_type):
    """
    Reads the inetd configuration file to determine whether there are any
    FTP/SSH connection and/or rate limits set in the end device's configuration.
    If a limit is found, the script will deactivate the specific line in the
    configuration to prevent any issues.
    Args:
        ssh(StartShell Object): Shell session to end device established earlier
        in execution.

        con_type(str): Specifies type of connection that will be established.
    Returns:
        None
    Raises:
        A general exception if shell commands fail to execute correctly or if
        a real exception is thrown due to some unknown error.
    """

    inetd = ss.run('cat /etc/inetd.conf', timeout=300)
    if not ss.last_ok:
        print('Error: failed to read /etc/inetd.conf, cant determine connection'
              ' limits')
        sys.exit(1)
    port_conf = []

    if con_type == 'ftp':
        port_conf.append(re.search(r'ftp stream tcp\/.*', inetd[1]).group(0))
        port_conf.append(re.search(r'ssh stream tcp\/.*', inetd[1]).group(0))
    else:
        port_conf.append(re.search(r'ssh stream tcp\/.*', inetd[1]).group(0))

    command_list = []
    for port in port_conf:
        config = re.split('/| ', port)
        p_name = config[0]
        con_lim = int(config[5])
        rate_lim = int(config[6])

        # check for presence of rate/connection limits
        try:
            if con_lim < 25:
                print('{} configured connection-limit is under 25'
                      .format(p_name.upper()))
                d_config = ss.run('cli -c "show configuration | display set '
                                  '| grep {} | grep connection-limit"'
                                  .format(p_name))
                if(ss.last_ok and
                   re.search(r'connection-limit', d_config[1]) != None):
                    d_config = d_config[1].split('\r\n')[1]
                    d_config = re.sub(' [0-9]+$', '', d_config)
                    d_config = re.sub('set', 'deactivate', d_config)
                    command_list.append('{};'.format(d_config))
                else:
                    raise Exception

            if rate_lim < 100:
                print('{} configured rate limit is under 100'
                      .format(p_name.upper()))
                d_config = ss.run('cli -c "show configuration | display set '
                                  '| grep {} | grep rate-limit"'
                                  .format(p_name))
                if(ss.last_ok and
                   re.search(r'rate-limit', d_config[1]) != None):
                    d_config = d_config[1].split('\r\n')[1]
                    d_config = re.sub(' [0-9]+$', '', d_config)
                    d_config = re.sub('set', 'deactivate', d_config)
                    command_list.append('{};'.format(d_config))
                else:
                    raise Exception

        except Exception:
            print('Error: failed to determine configured limits, '
                  'cannot proceed')
            sys.exit(1)

    try:
        # if limits were configured, deactivate them
        if command_list:
            ss.run('cli -c "edit;{}commit and-quit"'
                   .format(''.join(command_list)))

            if ss.last_ok:
                print('NOTICE: the configuration has been modified. '
                      'deactivated the limit(s) found')
            else:
                raise Exception

    except Exception:
        print('Error: failed to deactivate limits. Cannot proceed')
        sys.exit(1)


def remote_cleanup(ss, remotedir, file_name):
    """ delete tmp directory on remote host
    Args:
        dir(str) - remote directory to remove
    Returns:
        True if directory deletion was successful
        False if directory deletion was unsuccessful
    Raises:
        none
    """
    print('deleting remote tmp directory...')
    ss.run('rm -rf {}/splitcopy_{}'
           .format(remotedir, file_name), timeout=300)
    if not ss.last_ok:
        print('unable to delete the tmp directory on remote host,'
              ' delete it manually')
        return False
    return True


if __name__ == '__main__':
    main()
