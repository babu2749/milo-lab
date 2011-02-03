#!/usr/bin/python


from toolbox.database import SqliteDatabase
from toolbox.html_writer import HtmlWriter
from pygibbs import thermodynamics
from pygibbs.thermodynamic_constants import R, default_I, default_pH
from pygibbs.thermodynamic_constants import default_pMg, default_T
import pylab
from pygibbs.groups import GroupContribution
from pygibbs.kegg import Kegg
from toolbox.plotting import cdf
from SOAPpy import WSDL
import logging

def try_kegg_api():
    db = SqliteDatabase('../res/gibbs.sqlite')
    html_writer = HtmlWriter('../res/dG0_test.html')
    kegg = Kegg(db)
    G = GroupContribution(db, html_writer=html_writer, kegg=kegg)
    G.init()
    
    wsdl = 'http://soap.genome.jp/KEGG.wsdl'
    serv = WSDL.Proxy(wsdl)
    
    
    rid_file = open('../res/eco_rids.txt', 'w')
    rids = set()
    for x in serv.list_pathways('eco'):
        pathway_id = x['entry_id']
        for reaction_id in serv.get_reactions_by_pathway(pathway_id):
            rid = int(reaction_id[4:])
            if rid not in rids:
                rids.add(rid)
                rid_file.write('%d\n' % rid)
    rid_file.close()
            
    c_mid = 1e-3
    pH, pMg, I, T = (7.0, 3.0, 0.1, 298.15)
    
    rid2reversibility = {}
    misses = 0
    for rid in sorted(rids):
        try:
            r = CalculateReversability(rid, G, c_mid, pH, pMg, I, T)
            rid2reversibility[rid] = r
        except thermodynamics.MissingCompoundFormationEnergy:
            misses += 1
            continue
    
    print 'hits = %d, misses = %d' % len(rid2reversibility), misses
    median = pylab.median(rid2reversibility.values())
    print 'median = %.1f' % median

    pylab.figure()
    pylab.hold(True)
    cdf(rid2reversibility.values(), 'all reactions', 'r', show_median=True)
    pylab.show()
    

WATER = 1
HPLUS = 80


def GetConcentrationMap(kegg_handle):
    cmap = {}    
    for cid in kegg_handle.get_all_cids():
        lower, upper = kegg_handle.get_bounds(cid)
        if lower and upper:
            cmap[cid] = lower
    return cmap


def ConcentrationFactor(sparse_reaction,
                        concentration_map,
                        c_mid):
    factor = 0
    for cid, stoic in sparse_reaction.iteritems():
        concentration = concentration_map.get(cid, None) or c_mid
        factor += pylab.log(concentration) * stoic
    return factor

def CalculateReversability(rid, G, c_mid=1e-3, pH=default_pH, 
                           pMg=default_pMg, I=default_I, T=default_T,
                           concentration_map=None):
    cmap = concentration_map or {}
    dG0 = G.estimate_dG_keggrid(rid, pH, pMg, I, T)
    sparse = G.kegg().rid2sparse_reaction(rid)
    
    # remove H2O and H+ from the list of reactants since their
    # concentration is fixed
    sparse.pop(WATER, None)
    sparse.pop(HPLUS, None)

    cfactor = ConcentrationFactor(sparse, cmap, c_mid)
    sum_abs_s = sum([abs(x) for k, x in sparse.iteritems()
                     if k not in cmap])
    return 2 / pylab.log(10) * ((-dG0/(R*T) + cfactor) / sum_abs_s)

def calculate_reversibility_histogram(G, c_mid, pH, pMg, I, T, kegg, cmap):
    histogram = {}
    histogram['total'] = []
    hits = 0
    misses = 0
    for rid_flux_list in kegg.mid2rid_map.itervalues():
        if not rid_flux_list or len(rid_flux_list) < 2:
            continue
        for i, (rid, flux) in enumerate(rid_flux_list):
            try:
                r = flux * CalculateReversability(rid, G, c_mid, pH, pMg, I, T,
                                                  concentration_map=cmap)
                histogram.setdefault(i, []).append(r)
                if i > 1:
                    histogram['total'].append(r)
                hits += 1
            except thermodynamics.MissingCompoundFormationEnergy:
                misses += 1
                continue

    logging.info("Reactions with known dG0: %d" % hits)
    logging.info("Reactions with unknown dG0: %d" % misses)
    return histogram

def plot_histogram(histogram, html_writer, title='', max_pathway_length=8):
    fig = pylab.figure()
    pylab.hold(True)
    cdf(histogram[0], '1 (median=%.1f, N=%d)' % \
        (pylab.median(histogram[0]), len(histogram[0])), 'r')
    cdf(histogram[1], '2 (median=%.1f, N=%d)' % \
        (pylab.median(histogram[1]), len(histogram[1])), 'b')
    cdf(histogram['total'], '3-%d  (median=%.1f, N=%d)' % \
        (max_pathway_length, pylab.median(histogram['total']), len(histogram['total'])), 'g')
    pylab.xlim(-20, 20)
    pylab.xlabel('irreversability')
    pylab.ylabel('cumulative distribution')
    pylab.legend(loc='lower right')
    pylab.title(title)
    pylab.hold(False)
    return fig

def main():
    db = SqliteDatabase('../res/gibbs.sqlite')
    html_writer = HtmlWriter('../res/reversibility.html')
    kegg = Kegg(db)
    G = GroupContribution(db, html_writer=html_writer, kegg=kegg)
    G.init()
    c_mid = 1e-3
    pH, pMg, I, T = (7.0, 3.0, 0.1, 298.15)
    
    histogram = calculate_reversibility_histogram(G, c_mid, pH, pMg, I, T, kegg,
                                                  cmap=GetConcentrationMap(kegg))
    fig1 = plot_histogram(histogram, html_writer, title='With constraints on co-factors')
    html_writer.embed_matplotlib_figure(fig1, width=640, height=480)
    pylab.savefig('../res/reversibility1.pdf', figure=fig1, format='pdf')
    
    histogram = calculate_reversibility_histogram(G, c_mid, pH, pMg, I, T, kegg,
                                                  cmap={})
    fig2 = plot_histogram(histogram, html_writer, title='No constraints on co-factors')
    html_writer.embed_matplotlib_figure(fig2, width=640, height=480)
    pylab.savefig('../res/reversibility2.pdf', figure=fig1, format='pdf')
    
if __name__ == "__main__":
    #try_kegg_api()
    main()
