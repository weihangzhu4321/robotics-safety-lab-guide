/* C11: emit one synthetic Ethernet/IPv4/TCP SYN PCAP; no network APIs. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>
#endif

static FILE *exclusive_file(const char *path) {
#ifdef _WIN32
    /* Older Windows CRTs reject C11 fopen("wbx"); preserve exclusive creation. */
    int descriptor=_open(path,_O_WRONLY|_O_CREAT|_O_EXCL|_O_BINARY,_S_IREAD|_S_IWRITE);
    FILE *result;
    if(descriptor<0) { return NULL; }
    result=_fdopen(descriptor,"wb");
    if(!result) { _close(descriptor); }
    return result;
#else
    return fopen(path,"wbx");
#endif
}

static void be16(uint8_t *p, uint16_t n) { p[0]=(uint8_t)(n>>8); p[1]=(uint8_t)n; }
static void be32(uint8_t *p, uint32_t n) {
    p[0]=(uint8_t)(n>>24); p[1]=(uint8_t)(n>>16); p[2]=(uint8_t)(n>>8); p[3]=(uint8_t)n;
}
static void le16(uint8_t *p, uint16_t n) { p[0]=(uint8_t)n; p[1]=(uint8_t)(n>>8); }
static void le32(uint8_t *p, uint32_t n) {
    p[0]=(uint8_t)n; p[1]=(uint8_t)(n>>8); p[2]=(uint8_t)(n>>16); p[3]=(uint8_t)(n>>24);
}
static uint16_t checksum(const uint8_t *p, size_t n) {
    uint32_t s=0;
    while(n>1) { s+=((uint32_t)p[0]<<8)|p[1]; p+=2; n-=2; }
    if(n) { s+=(uint32_t)p[0]<<8; }
    while(s>>16) { s=(s&65535u)+(s>>16); }
    return (uint16_t)(~s);
}
int main(int argc, char **argv) {
    uint8_t frame[54]={0}, pseudo[32]={0}, global[24]={0}, record[16]={0};
    const uint8_t destination_mac[6]={2,0,0,0,0,32}, source_mac[6]={2,0,0,0,0,16};
    const uint8_t source_ip[4]={192,0,2,10}, destination_ip[4]={198,51,100,20};
    uint8_t *ip=frame+14, *tcp=frame+34;
    FILE *output;
    int failed;
    if(argc!=2) { fprintf(stderr,"Usage: offline_syn NEW_FILE.pcap\n"); return 2; }
    memcpy(frame,destination_mac,6); memcpy(frame+6,source_mac,6); be16(frame+12,0x0800);
    ip[0]=0x45; be16(ip+2,40); be16(ip+4,100); ip[8]=64; ip[9]=6;
    memcpy(ip+12,source_ip,4); memcpy(ip+16,destination_ip,4); be16(ip+10,checksum(ip,20));
    be16(tcp,40000); be16(tcp+2,80); be32(tcp+4,1000); tcp[12]=0x50; tcp[13]=2; be16(tcp+14,4096);
    memcpy(pseudo,source_ip,4); memcpy(pseudo+4,destination_ip,4); pseudo[9]=6; be16(pseudo+10,20);
    memcpy(pseudo+12,tcp,20); be16(tcp+16,checksum(pseudo,32));
    le32(global,0xa1b2c3d4u); le16(global+4,2); le16(global+6,4); le32(global+16,65535); le32(global+20,1);
    le32(record+8,54); le32(record+12,54);
    output=exclusive_file(argv[1]);
    if(!output) { perror("exclusive PCAP creation failed"); return 1; }
    failed=(fwrite(global,1,sizeof global,output)!=sizeof global);
    failed|=(fwrite(record,1,sizeof record,output)!=sizeof record);
    failed|=(fwrite(frame,1,sizeof frame,output)!=sizeof frame);
    if(fclose(output)!=0) { failed=1; }
    if(failed) { fprintf(stderr,"PCAP write failed; preserve partial file for diagnosis\n"); return 1; }
    puts("OFFLINE: one documentation-address SYN saved; no packet transmitted.");
    return 0;
}
