#!/usr/bin/perl
# Local, allowlisted PVE REST dispatch. Same handler contract as pvesh,
# but the JSON body travels on stdin so a root password is never in argv.
use strict;
use warnings;
use JSON::PP qw(decode_json encode_json);
use POSIX qw(uname dup dup2);
use FindBin qw($Bin);
use IPC::Open3;
use Symbol qw(gensym);
use Time::HiRes qw(time sleep alarm);

use Fcntl qw(S_ISREG O_RDWR O_NOFOLLOW LOCK_SH LOCK_NB);
$| = 1;
# PVE owns permissions for its files. Its unprivileged extraction worker
# must be able to traverse newly allocated rootfs directories, as in pvesh.
umask 0022;
my $raw = '';
my $session = @ARGV == 1 && $ARGV[0] eq '--read-session';
die "invalid\n" if @ARGV && !$session;
while (!$session && length($raw) <= 131072) {
    my $n = read(STDIN, my $chunk, 8192);
    last if !defined($n) || !$n;
    $raw .= $chunk;
}
sub read_allowed {
    my ($method, $path) = @_;
    return 0 if !defined($path) || ref($path) || !defined($method) || ref($method);
    my $node = qr/[A-Za-z0-9][A-Za-z0-9.-]{0,62}/;
    my $id = qr/[1-9][0-9]{2,8}/;
    return $method eq 'SCHEMA' ? $path =~ m{^/nodes/$node/lxc$}
        : $method eq 'GET' && $path =~ m{^/(?:version|nodes|pools(?:/[A-Za-z][A-Za-z0-9_.-]*)?|cluster/(?:status|resources|nextid|ha/resources/ct:$id)|nodes/$node/(?:status|services|storage(?:/[A-Za-z][A-Za-z0-9_.-]*/content)?|network|lxc/$id/(?:config|status/current|firewall/(?:options|rules))|tasks(?:/UPID:[A-Za-z0-9_.:\@-]+/status)?))$};
}
my $environment_ready = 0;
sub dispatch {
    my ($request) = @_;
    my $reply;
    die "invalid\n" if ref($request) ne 'HASH' || (join(',', sort keys %$request) ne 'method,params,path' && join(',', sort keys %$request) ne 'method,params,path,policy_revision');
    my ($method, $path, $params) = @$request{qw(method path params)};
    die "invalid\n" if ref($params) ne 'HASH' || ref($method) || ref($path) || !defined($path);
    my ($approval, $policy_lock);
    my $policy_deadline = time()+5;
    if ($method ne 'GET' && $method ne 'SCHEMA') {
        die "disabled\n" if exists($params->{force}) || exists($params->{restore});
        my ($host_node, $vmid) = $path =~ m{^/nodes/([A-Za-z0-9][A-Za-z0-9.-]{0,62})/lxc(?:/([1-9][0-9]{2,8})/)?};
        $vmid = $params->{vmid} if defined($host_node) && $path eq '/nodes/'.$host_node.'/lxc';
        die "disabled\n" unless defined($vmid) && $vmid =~ /^[1-9][0-9]{2,8}$/;
        sysopen($policy_lock, '/etc/proxmenux/native-lxc-policy.lock', O_RDWR|O_NOFOLLOW) or die "disabled\n";
        my @info = stat($policy_lock);
        die "disabled\n" unless @info && S_ISREG($info[2]) && $info[4] == 0 && ($info[2] & 07777) == 0600;
        while (!flock($policy_lock, LOCK_SH|LOCK_NB)) {
            die "timeout\n" if time() >= $policy_deadline;
            sleep(.02);
        }
        my ($writer, $reader); my $error = gensym(); my $pid;
        eval {
            local $SIG{ALRM} = sub { die "timeout\n"; };
            my $remaining = $policy_deadline-time(); die "timeout\n" if $remaining <= 0;
            alarm($remaining);
            $pid = open3($writer, $reader, $error, '/usr/bin/python3', '-B', $Bin.'/native_lxc_policy.py');
            print $writer encode_json({node=>$host_node,vmid=>0+$vmid,pool=>$params->{pool}//'',revision=>$request->{policy_revision}});
            close($writer);
            my $output = '';
            while (length($output) <= 4096) { my $n = read($reader, my $part, 4097-length($output)); last unless $n; $output .= $part; }
            close($reader);
            waitpid($pid, 0); die "disabled\n" if $? != 0 || length($output)>4096;
            $approval = decode_json($output);
            die "disabled\n" unless $approval->{ok};
            alarm(0);
        };
        if ($@) { alarm(0); if ($pid) { kill 'KILL', $pid; waitpid($pid, 0); } die "disabled\n"; }
    }
    # Every path is generated server-side. No proxy, arbitrary endpoint or DELETE.
    my $node = qr/[A-Za-z0-9][A-Za-z0-9.-]{0,62}/;
    my $id = qr/[1-9][0-9]{2,8}/;
    my $read = read_allowed('GET', $path);
    my $schema = $method eq 'SCHEMA' && $path =~ m{^/nodes/$node/lxc$};
    my $write = ($method eq 'POST' && $path =~ m{^/(?:nodes/$node/lxc(?:/$id/(?:status/start|firewall/rules))?|cluster/ha/resources)$})
        || ($method eq 'PUT' && $path =~ m{^/nodes/$node/lxc/$id/(?:config|firewall/options)$});
    die "invalid\n" unless ($method eq 'GET' && $read) || $schema || $write;
    require PVE::RPCEnvironment;
    require PVE::INotify;
    require PVE::API2;
    if (!$environment_ready) {
        PVE::RPCEnvironment->setup_default_cli_env();
        $environment_ready = 1;
    }
    my $local = PVE::INotify::nodename();
    if ($path =~ m{^/nodes/([^/]+)/}) {
        die "node\n" unless $1 eq $local;
    }
    if ($approval && !$approval->{legacy} && @{$approval->{protected_pools}}) {
        # Repeat dynamic pool protection in this final dispatcher. A failed
        # read is not an empty pool; alarm ends before the mutating handler.
        local $SIG{ALRM} = sub { die "timeout\n"; };
        my $remaining = $policy_deadline-time(); die "timeout\n" if $remaining <= 0;
        alarm($remaining);
        my $read_data = sub {
            my ($p) = @_; my $uri = {};
            my ($h, $i) = PVE::API2->find_handler('GET', $p, $uri);
            die "unsupported\n" unless $h && $i;
            return $h->handle($i, $uri);
        };
        my $pools = $read_data->('/pools'); die "invalid\n" unless ref($pools) eq 'ARRAY';
        my %names;
        for my $pool (@$pools) { die "invalid\n" unless ref($pool) eq 'HASH' && defined($pool->{poolid}) && !$names{$pool->{poolid}}++; }
        my ($vmid) = $path =~ m{/lxc/([1-9][0-9]{2,8})/}; $vmid //= $params->{vmid};
        for my $name (@{$approval->{protected_pools}}) {
            next unless $names{$name};
            my $pool = $read_data->('/pools/'.$name);
            die "invalid\n" unless ref($pool) eq 'HASH' && ref($pool->{members}) eq 'ARRAY';
            for my $member (@{$pool->{members}}) {
                die "invalid\n" unless ref($member) eq 'HASH' && defined($member->{type}) && $member->{type} =~ /^(?:lxc|qemu|storage)$/;
                if ($member->{type} ne 'storage') {
                    die "invalid\n" unless defined($member->{vmid}) && $member->{vmid} =~ /^[1-9][0-9]{2,8}$/;
                    die "protected\n" if $member->{vmid} == $vmid;
                }
            }
        }
        alarm(0);
    }
    my $uri = {};
    my ($handler, $info) = PVE::API2->find_handler($schema ? 'POST' : $method, $path, $uri);
    die "unsupported\n" unless $handler && $info;
    if ($schema) {
        my $props = $info->{parameters}->{properties};
        my $net = ref($props->{net0}->{format}) eq 'HASH' ? $props->{net0}->{format} : {};
        my $mp = ref($props->{mp0}->{format}) eq 'HASH' ? $props->{mp0}->{format} : {};
        my @uname = uname();
        $reply = { ok => JSON::PP::true, data => { properties => [sort keys %$props],
            network_fields => [sort keys %$net], mount_fields => [sort keys %$mp], architecture => $uname[4] } };
    } else {
        # Do not forward request values or PVE exception text to logs/responses.
        my $data;
        {
            # Redirect descriptors too: storage plugins launch child processes
            # whose fd1 otherwise bypasses a localized Perl STDOUT glob.
            my $saved_out = dup(1); my $saved_err = dup(2);
            open(my $null, '>', '/dev/null') or die "io\n";
            dup2(fileno($null), 1); dup2(fileno($null), 2);
            eval { $data = $handler->handle($info, { %$params, %$uri }); };
            my $error = $@;
            dup2($saved_out, 1); dup2($saved_err, 2);
            POSIX::close($saved_out); POSIX::close($saved_err); close($null);
            die $error if $error;
        }
        $reply = { ok => JSON::PP::true, data => $data };
    }
    return $reply;
}
sub respond {
    my ($raw, $read_only) = @_;
    my $reply;
    eval {
    die "invalid\n" if length($raw) > 131072;
    my $request = decode_json($raw);
    if (ref($request) eq 'HASH' && exists($request->{reads})) {
        die "invalid\n" unless join(',',sort keys %$request) eq 'reads'
            && ref($request->{reads}) eq 'ARRAY' && @{$request->{reads}} > 0 && @{$request->{reads}} <= 64;
        # Validate the entire read-only envelope before any API initialization.
        for my $item (@{$request->{reads}}) {
            die "invalid\n" unless ref($item) eq 'HASH' && join(',',sort keys %$item) eq 'method,params,path'
                && ref($item->{params}) eq 'HASH' && read_allowed($item->{method},$item->{path});
        }
        require PVE::INotify;
        my $local = PVE::INotify::nodename();
        for my $item (@{$request->{reads}}) {
            die "node\n" if $item->{path} =~ m{^/nodes/([^/]+)/} && $1 ne $local;
        }
        my @results;
        for my $item (@{$request->{reads}}) {
            my $value = eval { dispatch($item) };
            if ($@) { alarm(0); $value = {ok=>JSON::PP::false,error=>'pve_unavailable'}; }
            push @results,$value;
        }
        $reply = {ok=>JSON::PP::true,data=>\@results};
    } else {
        die "read only\n" if $read_only;
        $reply = dispatch($request);
    }
};
    if ($@) { alarm(0); return ({ok => JSON::PP::false, error => 'pve_unavailable'}, 1); }
    return ($reply, 0);
}
if ($session) {
    # Each frame is a complete independently validated read-only batch.
    # Never keep PVE state after this discovery, or accept a write frame.
    for (1..64) {
        my $frame = '';
        while (length($frame) <= 131072) {
            my $n = read(STDIN, my $byte, 1);
            exit 0 unless defined($n) && $n;
            last if $byte eq "\n";
            $frame .= $byte;
        }
        my ($reply, $failed) = respond($frame, 1);
        print encode_json($reply)."\n";
        exit 1 if $failed;
    }
} else {
    my ($reply, $failed) = respond($raw, 0);
    print encode_json($reply);
    exit $failed;
}
