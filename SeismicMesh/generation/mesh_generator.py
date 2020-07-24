# -----------------------------------------------------------------------------
# Copyright 2020 Keith Jared Roberts
#
# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING,
# BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
# OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# -----------------------------------------------------------------------------

import numpy as np
from mpi4py import MPI
import time

from . import utils as mutils
from .. import decomp
from .. import migration
from .. import geometry

from .cpp.delaunay_class import DelaunayTriangulation as DT2
from .cpp.delaunay_class3 import DelaunayTriangulation3 as DT3


class MeshGenerator:  # noqa: C901
    """Class constructor for :class:`MeshGenerator`. User can also register their callbacks to
       the sizing function :math:`f(h)` and signed distance function `f(d)` manually.

       :param SizingFunction: A :class:`MeshSizeFunction` object with populated fields and callbacks to `fd` and `fh`.
       :type SizingFunction:  A :class:`MeshSizeFunction` class object. Required if no `fd` or `fh` are passed.
       :param method: The back-end Delaunay triangulation algorithm to use. Either `qhull` or `cgal`. NB: qhull is 5 times slower than cgal. Default=`cgal`.
       :type method: A string either `qhull` or `cgal`, optional
       :param fd: A function that accepts an array of points and returns the signed distance to the boundary of the domain.
       :type fd: A function object, optional if no :class:`SizingFunction` is passed
       :param fh: A call-back function that accepts an array of points and returns an array of desired triangular mesh sizes close by to each point in the passed array.
       :type fh: A function object, optional if no :class:`SizingFunction` is passed.
       :param bbox: bounding box containing domain extents.
       :type bbox: tuple with size (2*dim). For example, in 2D `(zmin, zmax, xmin, xmax)`. Optional if no :class:`SizingFunction` is passed.
       :param hmin: minimum triangular edgelength populating the domain in meters.
       :type hmin: float64,optional if no :class:`SizingFunction` is passed.
       :param pfix: points that you wish you constrain in location.
       :type pfix: nested list [num_fixed_points x dim], optional

       :return: object populated with meta-data.
       :rtype: :class:`MeshGenerator` object
    """

    def __init__(
        self, SizingFunction=None, fd=None, fh=None, bbox=None, hmin=None, pfix=None,
    ):
        self.SizingFunction = SizingFunction
        self.fd = fd
        self.fh = fh
        self.bbox = bbox
        self.h0 = hmin
        self.pfix = pfix

    # SETTERS AND GETTERS
    @property
    def SizingFunction(self):
        return self.__SizingFunction

    @SizingFunction.setter
    def SizingFunction(self, value):
        self.__SizingFunction = value

    @property
    def fd(self):
        return self.__fd

    @fd.setter
    def fd(self, value):
        self.__fd = value

    @property
    def fh(self):
        return self.__fh

    @fh.setter
    def fh(self, value):
        self.__fh = value

    @property
    def bbox(self):
        return self.__bbox

    @bbox.setter
    def bbox(self, value):
        if value is None:
            self.__bbox = value
        else:
            assert (
                len(value) >= 4 and len(value) <= 6
            ), "bbox has wrong number of values. either 4 or 6."
            self.__bbox = value

    @property
    def hmin(self):
        return self.__hmin

    @hmin.setter
    def hmin(self, value):
        assert value > 0.0, "hmin must be non-zero"
        self.__hmin = value

    @property
    def pfix(self):
        return self.__pfix

    @pfix.setter
    def pfix(self, value):
        self.__pfix = value

    ### PRIVATE METHODS ###
    def __get_topology(self, dt):
        """ Get points and entities from CGAL:DelaunayTriangulation2/3 object"""
        p = dt.get_finite_vertices()
        if self.dim == 2:
            t = dt.get_finite_faces()
        elif self.dim == 3:
            t = dt.get_finite_cells()
        return p, t

    ### PUBLIC METHODS ###
    def build(  # noqa: ignore=C901
        self,
        SizingFunction=None,
        points=None,
        max_iter=50,
        nscreen=1,
        seed=None,
        COMM=None,
        axis=0,
        perform_checks=False,
        mesh_improvement=False,
        min_dh_bound=10,
        max_dh_bound=170,
        enforce_sdf=True,
    ):
        """
         Using callbacks to a sizing function and signed distance field build a simplical mesh.

        :param max_iter: maximum number of iterations, default=100
        :type max_iter: int, optional
        :param nscreen: output to screen every nscreen iterations (default==1)
        :type nscreen: int, optional
        :param seed: Random seed to ensure results are deterministic (default==None)
        :type seed: int, optional
        :param points: initial point distribution to commence mesh generation (default==None)
        :type points: numpy.ndarray[num_points x dimension], optional
        :param COMM: communicator for parallel execution (default==None)
        :type COMM: MPI4py communicator object generated when initializing MPI environment, required if parallel.
        :param axis: axis to decomp the domain wrt for parallel execution (default==0)
        :type axis: int, required if parallel.
        :param perform_checks: run serial mesh linting (default==False)
        :type perform_checks: logical, optional
        :param min_dh_bound: minimum dihedral angle allowed (default=5)
        :type min_dh_bound: float64, optional
        :param mesh_improvement: run 3D sliver perturbation mesh improvement (default=False)
        :type mesh_improvement: logical, optional
        :type enforce_sdf: whether to enforce domain boundaries with SDF (default=True)
        :param enforce_sdf: logical, optional

        :return: vertices of simplical mesh
        :rtype: numpy.ndarray[num_points x dimension]
        :return: cells of simplicial mesh
        :rtype: numpy.ndarray[num_cells x (dimension + 1)]
        """
        if self.SizingFunction is not None:
            # if :class:`SizingFunction` is passed, grab that data.
            _ef = self.SizingFunction
            fh = _ef.interpolant
            fd = _ef.fd
            bbox = _ef.bbox
            h0 = _ef.hmin
        else:
            # else it had to have been passed to constructor
            fh = self.fh
            fd = self.fd
            bbox = self.bbox
            h0 = self.hmin

        _pfix = self.pfix
        comm = COMM
        _axis = axis
        _points = points

        # configure parallel computing env.
        if comm is not None:
            PARALLEL = True
            rank = comm.Get_rank()
            size = comm.Get_size()
        else:
            PARALLEL = False
            rank = 0
            size = 1

        if size > 1 and mesh_improvement:
            raise Exception("Mesh improvement only works in serial.")

        # set random seed to ensure deterministic results for mesh generator
        if seed is not None:
            if rank == 0:
                print("Setting psuedo-random number seed to " + str(seed), flush=True)
            np.random.seed(seed)

        dim = int(len(bbox) / 2)
        bbox = np.array(bbox).reshape(-1, 2)

        L0mult = 1 + 0.4 / 2 ** (dim - 1)
        deltat = 0.1
        geps = 1e-1 * h0
        deps = np.sqrt(np.finfo(np.double).eps) * h0

        # select back-end CGAL call
        if dim == 2:
            DT = DT2
        elif dim == 3:
            DT = DT3

        if _pfix is not None:
            if PARALLEL:
                raise Exception("Fixed points are not yet supported in parallel.")
            else:
                pfix = np.array(_pfix, dtype="d")
                nfix = len(pfix)
            if rank == 0:
                print(
                    "Constraining " + str(nfix) + " fixed points..", flush=True,
                )
        else:
            pfix = np.empty((0, dim))
            nfix = 0

        if _points is None:
            # If the user has NOT supplied points
            if PARALLEL:
                # 1a. Localize mesh size function grid.
                fh = migration.localize_sizing_function(fh, h0, bbox, dim, _axis, comm)
                # 1b. Create initial points in parallel in local box owned by rank
                p = mutils.make_init_points(bbox, rank, size, _axis, h0, dim)
            else:
                # 1. Create initial distribution in bounding box (equilateral triangles)
                p = np.mgrid[
                    tuple(slice(min, max + h0, h0) for min, max in bbox)
                ].astype(float)
                p = p.reshape(dim, -1).T

            # 2. Remove points outside the region, apply the rejection method
            p = p[fd(p) < geps]  # Keep only d<0 points
            r0 = fh(p)
            r0m = r0.min()
            # Make sure decimation occurs uniformly accross ranks
            if PARALLEL:
                r0m = comm.allreduce(r0m, op=MPI.MIN)
            p = np.vstack(
                (pfix, p[np.random.rand(p.shape[0]) < r0m ** dim / r0 ** dim],)
            )
            if PARALLEL:
                # min x min y min z max x max y max z
                extent = [*np.amin(p, axis=0), *np.amax(p, axis=0)]
                extent[_axis] -= h0
                extent[_axis + dim] += h0
                extents = [comm.bcast(extent, r) for r in range(size)]
        else:
            # If the user has supplied initial points
            if PARALLEL:
                # Domain decompose and localize points
                p = None
                if rank == 0:
                    blocks, extents = decomp.blocker(
                        points=_points, rank=rank, nblocks=size, axis=_axis
                    )
                else:
                    blocks = None
                    extents = None
                # send points to each subdomain
                p, extents = migration.localize_points(blocks, extents, comm, dim)
                extent = extents[rank]
            else:
                p = _points

        N = len(p)

        count = 0
        print(
            "Commencing mesh generation with %d vertices on rank %d." % (N, rank),
            flush=True,
        )

        while True:

            # 3. Retriangulation by the Delaunay algorithm
            start = time.time()

            # Using CGAL's incremental Delaunay triangulation algorithm
            # Triangulate first iteration, the rest perform incremental operations.
            if count == 1:
                dt = DT().insert(p.flatten().tolist())

            # Get the current topology of the triangulation
            p, t = self._get_topology(dt)

            if PARALLEL:
                exports = migration.enqueue(extents, p, t, rank, size, dim=dim)
                recv = migration.exchange(comm, rank, size, exports, dim=dim)
                recv_ix = len(recv)

                # insert halo points. NB: can add a flag to insert to make info negative for halo vertices
                dt.insert(recv.flatten().tolist())
                p, t = self._get_topology(dt)
                # remove entities will all vertices outside local block
                remove = geometry.remove_external_entities(p, t, extent, dim=dim)
                dt.remove(remove)
                p, t = self._get_topology(dt)

            pmid = p[t].sum(1) / (dim + 1)  # Compute centroids
            remove = np.unique(t[fd(pmid) < -geps])  # Keep interior triangles
            dt.remove(remove)  # remove the points associated with these simplices

            # 4. Describe each bar by a unique pair of nodes
            p, t = self._get_topology(dt)
            bars = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
            if dim == 3:
                bars = np.concatenate(
                    (bars, t[:, [0, 3]], t[:, [1, 3]], t[:, [2, 3]]), axis=0
                )
            bars = mutils.unique_rows(bars)[0]  # Bars as node pairs

            # Periodic sliver removal
            num_move = 0
            if mesh_improvement and count != (max_iter - 1) and dim == 3:
                # calculate dihedral angles in mesh
                dh_angles = np.rad2deg(geometry.calc_dihedral_angles(p, t))
                outOfBounds = np.argwhere(
                    (dh_angles[:, 0] < min_dh_bound) | (dh_angles[:, 0] > max_dh_bound)
                )
                eleNums = np.floor(outOfBounds / 6).astype("int")
                eleNums, ix = np.unique(eleNums, return_index=True)

                if count % nscreen == 0:
                    print(
                        "On rank: "
                        + str(rank)
                        + " There are "
                        + str(len(eleNums))
                        + " slivers...",
                        flush=True,
                    )

                move = t[eleNums, 0]
                num_move = move.size
                if PARALLEL:
                    g_num_move = comm.allreduce(num_move, op=MPI.SUM)
                    if num_move == 0 and g_num_move != 1:
                        if count % nscreen == 0:
                            print("Rank " + str(rank) + " is locked...", flush=True)
                        nfix = N
                    if g_num_move == 0:
                        if rank == 0:
                            print(
                                "Termination reached...No slivers detected!", flush=True
                            )
                        p, t = migration.aggregate(p, t, comm, size, rank, dim=dim)
                        return p, t
                else:
                    if num_move == 0:
                        print("Termination reached...No slivers detected!", flush=True)
                        if rank == 0 and perform_checks:
                            p, t = geometry.linter(p, t, dim=dim)
                        elif rank == 0:
                            p, t, _ = geometry.fixmesh(p, t, dim=dim, delunused=True)
                        return p, t

                p0, p1, p2, p3 = (
                    p[t[eleNums, 0], :],
                    p[t[eleNums, 1], :],
                    p[t[eleNums, 2], :],
                    p[t[eleNums, 3], :],
                )

                # perturb vector is based on INCREASING circumsphere's radius
                perturb = geometry.calc_circumsphere_grad(p0, p1, p2, p3)
                perturb[np.isinf(perturb)] = 1.0

                # normalize
                perturb_norm = np.sum(np.abs(perturb) ** 2, axis=-1) ** (1.0 / 2)
                perturb /= perturb_norm[:, None]

                # perturb % of local mesh size
                new_pos = p[move] + 0.10 * h0 * perturb

                # incrementally move the points
                dt.move(new_pos.flatten().tolist(), move)

            p, t = self._get_topology(dt)

            if not mesh_improvement:
                # 6. Move mesh points based on bar lengths L and forces F
                barvec = p[bars[:, 0]] - p[bars[:, 1]]  # List of bar vectors
                L = np.sqrt((barvec ** 2).sum(1))  # L = Bar lengths
                hbars = fh(p[bars].sum(1) / 2)
                L0 = (
                    hbars
                    * L0mult
                    * ((L ** dim).sum() / (hbars ** dim).sum()) ** (1.0 / dim)
                )
                F = L0 - L
                F[F < 0] = 0  # Bar forces (scalars)
                Fvec = (
                    F[:, None] / L[:, None].dot(np.ones((1, dim))) * barvec
                )  # Bar forces (x,y components)

                Ftot = mutils.dense(
                    bars[:, [0] * dim + [1] * dim],
                    np.repeat([list(range(dim)) * 2], len(F), axis=0),
                    np.hstack((Fvec, -Fvec)),
                    shape=(N, dim),
                )

                Ftot[:nfix] = 0  # Force = 0 at fixed points

                new_pos = p + deltat * Ftot
                moved = np.arange(len(p))  # move only points that actually moved a bit
                dt.move(new_pos, moved)
                p, t = self._get_topology(dt)

            else:
                # no movement if mesh improvement (from forces)
                maxdp = 0.0

            # 7. Bring outside points back to the boundary
            d = fd(p)
            ix = d > 0  # Find points outside (d>0)

            if ix.any() and enforce_sdf:

                def deps_vec(i):
                    a = [0] * dim
                    a[i] = deps
                    return a

                dgrads = [(fd(p[ix] + deps_vec(i)) - d[ix]) / deps for i in range(dim)]
                dgrad2 = sum(dgrad ** 2 for dgrad in dgrads)
                dgrad2 = np.where(dgrad2 < deps, deps, dgrad2)
                new_pos = p[ix] - (d[ix] * np.vstack(dgrads) / dgrad2).T
                # move the points back into the domain!
                dt.move(new_pos.flatten().tolist(), ix)
                p, t = self._get_topology(dt)

            if count % nscreen == 0 and rank == 0 and not mesh_improvement:
                maxdp = deltat * np.sqrt((Ftot ** 2).sum(1)).max()

            # 8. Number of iterations reached
            if count == max_iter - 1:
                if rank == 0:
                    print(
                        "Termination reached...maximum number of iterations reached.",
                        flush=True,
                    )
                if PARALLEL:
                    p, t = migration.aggregate(p, t, comm, size, rank, dim=dim)
                if rank == 0 and perform_checks:
                    p, t = geometry.linter(p, t, dim=dim)
                break

            # 9. Delete ghost points
            if PARALLEL:
                # NEED TO HAVE LABEL TO ADDED GHOST POINTS
                # dt.remove(inv[-recv_ix::])
                comm.barrier()

            if count % nscreen == 0 and rank == 0:
                if PARALLEL:
                    print("On rank 0: ", flush=True)
                print(
                    "Iteration #%d, max movement is %f, there are %d vertices and %d cells"
                    % (count + 1, maxdp, len(p), len(t)),
                    flush=True,
                )

            count += 1

            end = time.time()
            if rank == 0 and count % nscreen == 0:
                print("     Elapsed wall-clock time %f : " % (end - start), flush=True)

        return p, t
