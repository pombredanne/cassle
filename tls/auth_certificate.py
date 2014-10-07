###############################################################################################
### Name: auth_certificate.py
### Author: Alvaro Felipe Melchor - alvaro.felipe91@gmail.com
### Twitter : @alvaro_fe
### University of Alcala de Henares
###############################################################################################



#Here all related with the verification through certficate

#from utils import util
import M2Crypto.X509
from M2Crypto.X509 import FORMAT_DER

import logging
import threading
import nss.nss as nss
import os
import platform
from pync import Notifier
from termcolor import colored
from db.database import database
from tls.ocsp import Ocsp
import subprocess, tempfile
from nss.error import NSPRError
from  pyasn1.codec.der import decoder, encoder
from pyasn1_modules import rfc2560, rfc2459
from pyasn1.type import univ
import hashlib
#TODO add verification based in the configuration file


from config import Config

#Configuration stuff
f = file('config/config.cfg')
cfg = Config(f)
f.close()
#


intended_usage = nss.certificateUsageSSLServer

########      LOG     #########
LOGFILENAME = 'cert.log'
LOG_DIR = cfg.config.LOG_DIR
try:
    os.mkdir(os.path.expanduser(LOG_DIR))
except OSError:
    # This error means that the directoy exists
    pass
logPath = os.path.expanduser(LOG_DIR) + LOGFILENAME
logging.basicConfig(filename=logPath,format='%(asctime)s --  %(levelname)s:%(message)s', level=logging.INFO)
########      LOG     #########


system = platform.system()

#TODO # add configuration and log and sendemail when a mitm happened
# TODO I am having some issues to gather certificate some are missed

class AuthCertificate(threading.Thread):
    """
    This class validate the authentication through certificate
    """

    def __init__(self, certificates,queue, screen_lock):

        #The queue is used to return data to the main thread
        threading.Thread.__init__(self)
        self.lock = screen_lock
        self.queue = queue
        self.certs = certificates

        self.cert_nss = list()

        self.certdb_dir = os.path.expanduser(cfg.config.NSS_DB_DIR)

        nss.nss_init(self.certdb_dir)
        self.certdb = nss.get_default_certdb()

        for i in xrange(0,len(self.certs)):
            try:
                self.cert_nss.append(nss.Certificate(self.certs[i],self.certdb))
            except:
                print 'Exception here'
                break

        try:
            self.ocsp =  Ocsp(self.certs[1],self.certs[0])
        except:
            pass

        db_name = cfg.db.db_name
        self.db_pin = database(db_name, cfg.db.coll_name_pinning)
        self.db_rfc = database(db_name, cfg.db.coll_name_log)
        self.db_blacklist = database(db_name, "blacklist")


    def run(self):
        #TODO add here execution based on the configuration file
        if len(self.cert_nss) == 1:
            cad =  'We need more information to validate the cerfificate --> ' + self.cert_nss[0].make_ca_nickname()
            with self.lock:
                print colored(cad,'red')
            return
        self.verify_cert_through_rfc()
        self.verify_dnssec_tlsa()
        self.verify_cert_with_pinning()
        self.verify_cert_with_icsy_notary()
        self.verify_ocsp()
        self.verify_ct()
        self.verify_ssl_blacklist()


    def verify_ssl_blacklist(self):
        s = hashlib.new("sha1")
        s.update(self.certs[0])
        fingerprint = s.hexdigest()
        query = self.db_blacklist.get(fingerprint)
        if query == None:
            with self.lock:
                print 'You are safe againts SSL-BLACKLIST database'
        else:
            with self.lock:
                print 'You are using a Certificate that match with malware-certificate'
        pass

    """
    Methods that implement verification using the certificate
    """
    def verify_ct(self):
        #self.ocsp.check_certificate_transparency()
        cert, _ = decoder.decode(self.certs[0],asn1Spec=rfc2459.Certificate())
        tbsCertificate = cert.getComponentByName('tbsCertificate')
        extensions = tbsCertificate.getComponentByName('extensions')
        sct = None
        for ext in extensions:
            if ext.getComponentByPosition(0) == univ.ObjectIdentifier((1,3,6,1,4,1,11129,2,4,2)):
                sct =  str(ext.getComponentByPosition(2)).encode('hex')
        if sct != None:
            with self.lock:
                print 'Signed Certificate Timestamp found ' + sct
        else:
            s = self.ocsp.check_certificate_transparency()
            if s != None:
                with self.lock:
                    print s


    def verify_dnssec_tlsa(self):
        import dns.resolver
        import hashlib
        try:
            url = self.cert_nss[0].subject_common_name
        except IndexError:
            return
        if url[0] == '*':
            url = url.replace('*', 'www')
        try:
            #print '_443._tcp. + url
            answer = dns.resolver.query('_443._tcp.' + url, 'TLSA')
            answer = [str(ans) for ans in answer][0].split(' ')
            hash_tlsa = answer[len(answer) - 1]
            s = hashlib.new('sha256')
            s.update(self.certs[0])
            res = s.hexdigest()
            if res == hash_tlsa:
                with self.lock:
                    print colored('The certificate with id %s is safe through DANE' % self.cert_nss[0].serial_number, 'magenta')
            else:
                with self.lock:
                    print colored('The certificate with id %s does not implement DANE' % self.cert_nss[0].serial_number, 'white')
        except:
            pass


    def verify_ocsp(self):
        status, certId = self.ocsp.check_ocsp()
        if status == None:
            with self.lock:
                print colored('The certificate with id  %s does not have OCSP URI' % self.cert_nss[0].serial_number,'white')
            return
        if status == 'revoked':
            self._notify_mitm(title='OCSP-MITM')
            with self.lock:
                print colored('This certificate with id  %s is revoked' % certId,'red')
        else:
            with self.lock:
                print colored('This certificate with id %s is not revoked' % certId,'cyan')


    def verify_cert_through_rfc(self):
            """
            This function try verify the certificate through RFC especification. We are using NSS to do it
            """
            approved_usage = not intended_usage
            try:
                # Turns on OCSP checking for the given certificate database. But only in the database not in the actual certificate of our site
                nss.enable_ocsp_checking(self.certdb)
                cert = self.cert_nss[0]
                ca_name = cert.make_ca_nickname()
                # Verify a certificate by checking if it's valid and if we trust the issuer. Here we are validating our certificate for SSLServer 
                approved_usage = cert.verify_now(self.certdb,True,intended_usage,None)

            except NSPRError:
                #Error ocurred maybe is due to a missed intermediate certificate so we should added but without confidence on it
                length = len(self.certs)
                self._add_certiticate_to_nssdb(1,name=cert.issuer.common_name)
                if length == 4:
                    inter = self.cert_nss[1]
                    self._add_certiticate_to_nssdb(2,name=inter.issuer.common_name)
                try:
                    approved_usage = cert.verify_now(self.certdb,True, intended_usage, None)
                except NSPRError:
                    pass

            serial = str(cert.serial_number)

            if approved_usage & intended_usage:
                with self.lock:
                    print colored('This certificate %s is safe through the RFC process ' % (serial + ' - ' + ca_name),'green')
            else:
                with self.lock:
                    print colored('This certificate %s is not safe through the RFC process ' % (serial + ' - ' + ca_name),'red')
                self._log_fail()
                self._notify_mitm(title='RFC-MITM')


    def verify_cert_with_pinning(self):
        from Crypto.Util.asn1 import DerSequence
        import sha3
        s = hashlib.new("sha3_512")
        #TODO change the _id in the database should be only a string
        try:
            # We extract SubjectPublicKeyInfo. Why? Because everybody say that is the best part of the certificate
            #to do that

            der = self.certs[0]
            cert_dec = DerSequence()
            cert_dec.decode(der)
            tbsCertificate = DerSequence()
            tbsCertificate.decode(cert_dec[0])
            spki = tbsCertificate[6]

            s.update(spki)
            hash_t = s.hexdigest()
            cert = self.cert_nss[0]
            serial = cert.serial_number
            _id = str(serial) + ' - ' + cert.make_ca_nickname()
            exist = self.db_pin.get(_id)
            if exist == None:
                # That means that the certificate is not in the database, it's the first time it was seen
                self.db_pin.set_pin(hash_t, _id)
                cad = "%s first seen" % _id
                with self.lock:
                    print colored(cad,'yellow')
            else:
                # Exist so we have to ensure it's correct
                correct = self.db_pin.compare(_id, hash_t)
                if correct == False:
                    cad = 'This certificate %s changed' % _id
                    with self.lock:
                        print colored(cad,'red')
                    self._notify_mitm(title=_id)

                else:
                    cad = 'Nothing changed ' + _id
                    with self.lock:
                        print colored(cad,'yellow')
        except Exception:
            pass


    def verify_cert_with_icsy_notary(self):
        import dns
        from dns import resolver
        cert = self.cert_nss[0]
        s = hashlib.new("sha1")
        s.update(self.certs[0])
        address = s.hexdigest()+'.notary.icsi.berkeley.edu'
        try:
            result =  resolver.query(address,rdtype=dns.rdatatype.TXT)[0].__str__().split()
        except:
            #icsy_notary doesn't have that certificate
            return
        validated = int(result[4].split('=')[1][0])
        first_seen = int(result[1].split('=')[1])
        last_seen = int(result[2].split('=')[1])
        times_seen = int(result[3].split('=')[1])
        if validated is not 1:
            cad = "This certificate %s is NOT safe through icsi_notary" % (cert.make_ca_nickname())
            with self.lock:
                print colored(cad,'red')
        else:
            s = last_seen - first_seen  + 1
            if s - times_seen > 1:
                cad = "This certificate %s is not ENOUGH secure according to icsi_notary" % (cert.make_ca_nickname())
                with self.lock:
                    print colored(cad,'red')
            else:
                cad = "This certificate %s IS SECURE through icsi_notary" % (cert.make_ca_nickname())
                with self.lock:
                    print colored(cad,'blue')


    def _log_fail(self):
            cn_cert = self.cert_nss[0]
            name = cn_cert.make_ca_nickname()
            exist = self.db_rfc.get(name)
            if exist is None:
                self.db_rfc.set_rfc(cn_cert.make_ca_nickname())
                # print cn_cert
                logging.info("You don't trust in this certificate when you connected to %s \n %s",
                                name, cn_cert)

    def _notify_mitm(self,title):
        if system == "Darwin":
            Notifier.notify("MITM",title=title)
        else:
            pass

    def _add_certiticate_to_nssdb(self,cert,name=None,trust=False):
        """
        This function add intermediate certificate to NSS-DB only to verify. We don't trust on it
        If you want to trust in it, only set trust param to True
        """

        if not os.path.exists(os.getcwd() +'/tmp'):
            os.mkdir(os.getcwd() + '/tmp')

        with tempfile.NamedTemporaryFile(dir=os.getcwd() +'/tmp',suffix='crt') as tmp:
            try:
                tmp.write(M2Crypto.X509.load_cert_string(self.certs[cert],FORMAT_DER).as_pem())
            except:
                return
            tmp.flush
            tmp.seek(0)
            if not trust:
                subprocess.call(["certutil", "-A","-n",name,'-t',',,,','-a','-i',tmp.name,'-d',self.certdb_dir])
            else:
                subprocess.call(["certutil", "-A","-n",name,'-t','C,,,','-a','-i',tmp.name,'-d',self.certdb_dir])
        return


