"""Read-only, day-specific ROS1 LAN parameter validation."""
import datetime, ipaddress, json, pathlib, re, subprocess, sys
def validate(path):
    fields={}
    for line in pathlib.Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        m=re.fullmatch(r'(CHECK_DATE|VM_IP|ROBOT_IP|MASTER_IP)=([A-Za-z0-9._:/-]+)',line)
        if not m or m[1] in fields: raise ValueError('Only four unique plain settings keys are allowed')
        fields[m[1]]=m[2]
    if set(fields)!={'CHECK_DATE','VM_IP','ROBOT_IP','MASTER_IP'}: raise ValueError('Missing settings')
    today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
    if fields['CHECK_DATE']!=today:raise ValueError('Recheck today date and addresses onsite')
    a,b,m=[ipaddress.IPv4Address(fields[k]) for k in ('VM_IP','ROBOT_IP','MASTER_IP')]
    private=[ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
    if a==b or m not in (a,b) or not all(any(x in n for n in private) for x in (a,b)):
        raise ValueError('Distinct RFC1918 A/B addresses required; master must be A or B')
    def read(args):return json.loads(subprocess.check_output(args,text=True,timeout=5))
    interfaces=read(['ip','-j','-4','addr','show'])
    routes=read(['ip','-j','-4','route','get',str(b)])
    if len(routes)!=1:raise ValueError('Route is ambiguous')
    r=routes[0]
    if r.get('gateway') or r.get('type') in ('local','broadcast','multicast','unreachable','blackhole','prohibit'):
        raise ValueError('Robot must be directly connected on the lab LAN')
    if r.get('prefsrc',r.get('src'))!=str(a):raise ValueError('Wrong actual egress source')
    local=[(i,x) for i in interfaces for x in i.get('addr_info',[]) if i.get('ifname')==r.get('dev') and x.get('local')==str(a)]
    if len(local)!=1:raise ValueError('VM_IP must belong to the actual egress interface')
    i,x=local[0];net=ipaddress.ip_network(str(a)+'/'+str(x['prefixlen']),strict=False)
    if b not in net or b in (net.network_address,net.broadcast_address):raise ValueError('Robot address not usable in direct subnet')
    if any(x.get('local')==str(b) for i in interfaces for x in i.get('addr_info',[])):raise ValueError('Robot address belongs to this VM')
    if i.get('operstate')!='UP' or 'UP' not in i.get('flags',[]):raise ValueError('Physical interface link is down')
    return fields
if __name__=='__main__':
    try:
        f=validate(sys.argv[1]);print(f['VM_IP'],f['ROBOT_IP'],f['MASTER_IP'])
    except Exception as e:
        print('NETWORK_CHECK_FAILED: '+str(e),file=sys.stderr);raise SystemExit(2)
