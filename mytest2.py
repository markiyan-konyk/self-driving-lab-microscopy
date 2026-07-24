from DG1022Z import DG1022Z

dg = DG1022Z()
dg._open()
dg.dcinit()
dg.dcupdate(1, 2.0)
dg.dcupdate(2, 2.0)
dg._close()