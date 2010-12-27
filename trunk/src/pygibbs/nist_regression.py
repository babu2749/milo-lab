import logging, sys, csv
from pygibbs.nist import Nist
from pygibbs.dissociation_constants import DissociationConstants
from pygibbs.kegg import Kegg
from toolbox.database import SqliteDatabase
from toolbox.util import _mkdir
from toolbox.html_writer import HtmlWriter
from pygibbs.group_decomposition import GroupDecomposer
from pygibbs import pseudoisomer

class NistRegression(object):
    
    def __init__(self, db, html_writer):
        self.db = db
        self.html_writer = html_writer
        self.kegg = Kegg()
        self.nist = Nist(self.kegg)
        dissociation = DissociationConstants(self.db, self.html_writer, self.kegg)
        dissociation.LoadValuesToDB('../data/thermodynamics/pKa_with_cids.csv')
        self.cid2pKa_list = dissociation.GetAllpKas()
        
    def Nist_pKas(self):
        group_decomposer = GroupDecomposer.FromDatabase(self.db)
        cids_in_nist = set(self.nist.cid2count.keys())
        
        html_writer.write('CIDs with pKa: %d<br>\n' % len(self.cid2pKa_list))
        html_writer.write('CIDs in NIST: %d<br>\n' % len(cids_in_nist))
        html_writer.write('CIDs in NIST with pKas: %d<br>\n' % \
                          len(cids_in_nist.intersection(self.cid2pKa_list.keys())))
        
        html_writer.write('All CIDs in NIST: <br>\n')
        html_writer.write('<table border="1">\n')
        html_writer.write('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>' % ("CID", "NAME", "COUNT", "REMARK"))
        for cid, count in sorted(self.nist.cid2count.iteritems()):
            if cid not in self.cid2pKa_list:
                self.html_writer.write('<tr><td>C%05d</td><td>%s</td><td>%d</td><td>' % (cid, self.kegg.cid2name(cid), count))
                try:
                    mol = self.kegg.cid2mol(cid)
                    decomposition = group_decomposer.Decompose(mol, ignore_protonations=True, strict=True)
        
                    if len(decomposition.PseudoisomerVectors()) > 1:
                        self.html_writer.write('should have pKas')
                    else:
                        self.html_writer.write('doesn\'t have pKas')
                    self.html_writer.embed_molecule_as_png(kegg.cid2mol(cid), 'png/C%05d.png' % cid)
                
                except Exception:
                    self.html_writer.write('cannot decompose')
                self.html_writer.write('</td></tr>\n')
        
        self.html_writer.write('</table>\n')

    def Calculate_pKa_and_pKMg(self, filename="../data/thermodynamics/dG0.csv"):
        cid2pmap = {}
        smiles_dict = {}
        
        for row in csv.DictReader(open(filename, 'r')):
            #smiles, cid, compound_name, dG0, unused_dH0, charge, hydrogens, Mg, use_for, ref, unused_assumption 
            name = "%s (z=%s, nH=%s, nMg=%s)" % (row['compound name'], row['charge'], row['hydrogens'], row['Mg'])
            logging.info('reading data for ' + name)
    
            if not row['dG0']:
                continue
    
            if (row['use for'] == "skip"):
                continue
                
            try:
                dG0 = float(row['dG0'])
            except ValueError:
                raise Exception("Invalid dG0: " + str(dG0))
    
            if (row['use for'] == "test"):
                pass
            elif (row['use for'] == "train"):
                pass
            else:
                raise Exception("Unknown usage flag: " + row['use for'])
    
            if row['cid']:
                cid = int(row['cid'])
                try:
                    nH = int(row['hydrogens'])
                    z = int(row['charge'])
                    nMg = int(row['Mg'])
                except ValueError:
                    raise Exception("can't read the data about %s" % (row['compound name']))
                cid2pmap.setdefault(cid, pseudoisomer.PseudoisomerMap())
                cid2pmap[cid].Add(nH, z, nMg, dG0)
    
            if row['smiles']:
                smiles_dict[cid, nH, z, nMg] = row['smiles']
            else: 
                smiles_dict[cid, nH, z, nMg] = ''
    
        #csv_writer = csv.writer(open('../res/pKa_from_dG0.csv', 'w'))
        
        self.html_writer.write('<table border="1">\n<tr><td>' + 
                          '</td><td>'.join(['CID', 'name', 'formula', 'nH', 'charge', 'nMg', 'dG0_f', 'pKa', 'pK_Mg']) + 
                          '</td></tr>\n')
        for cid in sorted(cid2pmap.keys()):
            #step = 1
            for nH, z, nMg, dG0 in sorted(cid2pmap[cid].ToMatrix(), key=lambda x:(-x[2], -x[0])):
                pKa = cid2pmap[cid].GetpKa(nH, z, nMg)
                pK_Mg = cid2pmap[cid].GetpK_Mg(nH, z, nMg)
                self.html_writer.write('<tr><td>')
                self.html_writer.write('</td><td>'.join(["C%05d" % cid, 
                                                    kegg.cid2name(cid) or "?", 
                                                    kegg.cid2formula(cid) or "?", 
                                                    str(nH), str(z), str(nMg), 
                                                    "%.1f" % dG0,
                                                    str(pKa),
                                                    str(pK_Mg)]))
                #if not nMg and cid not in cid2pKa_list:
                #    csv_writer.writerow([cid, kegg.cid2name(cid), kegg.cid2formula(cid), step, None, "%.2f" % pKa, smiles_dict[cid, nH+1, z+1, nMg], smiles_dict[cid, nH, z, nMg]])
                #    step += 1
                self.html_writer.write('</td></tr>\n')
        self.html_writer.write('</table>\n')

if (__name__ == "__main__"):
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _mkdir('../res/nist/png')
    html_writer = HtmlWriter("../res/nist/regression.html")
    db = SqliteDatabase('../res/gibbs.sqlite')

    nist_regression = NistRegression(db, html_writer)
    nist_regression.Nist_pKas()
    nist_regression.Calculate_pKa_and_pKMg()
    
    html_writer.close()